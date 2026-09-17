"""Batch-first terminology, sequential chapter chunks, quality repair and reconciliation."""

from __future__ import annotations

import asyncio

from . import prompts
from .dictionary import (
    export_dictionary,
    load_legacy,
    quantity_source_problem,
    relevant,
    sanity,
    source_name,
    source_problem,
    term_problem,
    terminology_gaps,
)
from .models import (
    PARSER_VERSION,
    Alignment,
    Candidates,
    Context,
    Evidence,
    Issue,
    Repair,
    Resolution,
    Translation,
    Validation,
)
from .parsing import make_chunks, validate_alignment, validate_inputs
from .store import digest


class QualityError(RuntimeError):
    pass


def pages(items, budget=18000):
    """Bound evidence prompts by content size, never by corresponding RAW/VP offsets."""
    page, size = [], 0
    for item in items:
        length = len(str(item))
        if page and size + length > budget:
            yield page
            page, size = [], 0
        page.append(item)
        size += length
    if page:
        yield page


def chapter_identity(chapter, field="number"):
    """Persist original numbering, adding scope only when the input has a volume."""
    identity = {field: chapter.number}
    if chapter.volume is not None:
        identity["volume"] = chapter.volume
    return identity


class Pipeline:
    def __init__(self, store, scheduler):
        self.store, self.scheduler = store, scheduler
        self.terms = {}
        self.pairs = []
        self.alignments = {}
        self.chunks = {}
        self.missed = {}
        self.activity = {}
        self.term_lock = asyncio.Lock()
        self.cache_keys = {}

    def progress(self, stage, **fields):
        self.store.progress(stage=stage, **fields)

    async def ai(self, task, instruction, payload, schema):
        key = digest(
            {
                "task": task,
                "instruction": instruction,
                "payload": payload,
                "schema": schema.model_json_schema(),
            }
        )
        self.cache_keys.setdefault(task, set()).add(key)
        cached = self.store.read(f"cache/{key}.json")
        if cached is not None:
            self.store.diagnostic(
                {
                    "task": task,
                    "status": "cached",
                    "message": "Reused completed checkpoint; no API request",
                }
            )
            return schema.model_validate(cached)
        result = await self.scheduler.request(
            instruction,
            payload,
            schema,
            lambda meta: self.store.diagnostic({"task": task, **meta}),
        )
        self.store.write(f"cache/{key}.json", result.model_dump())
        return result

    def fail(self, task, message):
        # Unfinalized, semantically invalid responses must not poison every resume.
        for operation, keys in self.cache_keys.items():
            if operation.startswith(task):
                for key in keys:
                    (self.store.path / f"cache/{key}.json").unlink(missing_ok=True)
        raise QualityError(message)

    async def parallel(self, items, function):
        # Only a small fixed worker pool, not one task per chapter/term.
        iterator = iter(items)

        async def worker():
            for item in iterator:
                await function(item)

        tasks = [asyncio.create_task(worker()) for _ in range(self.scheduler.concurrency)]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def align(self, pair):
        raw, vp = pair
        payload = {
            "raw": {
                **raw.model_dump(),
                "paragraphs": [{"id": i, "text": text} for i, text in enumerate(raw.paragraphs)],
            },
            "vietphrase": {
                **vp.model_dump(),
                "paragraphs": [{"id": i, "text": text} for i, text in enumerate(vp.paragraphs)],
            },
            "paragraph_ids": "zero based",
            "chapter_order_confirmed": True,
        }
        task = f"chapter:{raw.key}:alignment"
        alignment = await self.ai(task, prompts.ALIGN, payload, Alignment)
        for attempt in range(3):
            try:
                validate_alignment(alignment, raw, vp)
                break
            except ValueError as exc:
                self.store.write(
                    f"alignment-rejections/{raw.key}-{attempt}.json",
                    {
                        "error": str(exc),
                        "response": alignment.model_dump(),
                    },
                )
                if not alignment.confirmed or attempt == 2:
                    self.fail(task, f"Chapter {raw.key}: {exc}")
                alignment = await self.ai(
                    task + f":repair:{attempt}",
                    prompts.ALIGN,
                    {
                        **payload,
                        "structural_error": str(exc),
                        "previous_alignment": alignment.model_dump(),
                        "repair_instruction": "Correct the semantic alignment. Enumerate EVERY paragraph ID, not just range endpoints. Do not omit, duplicate, reorder or fabricate paragraph IDs.",
                    },
                    Alignment,
                )
        self.alignments[raw.key] = alignment
        self.chunks[raw.key] = make_chunks(alignment, raw, vp)
        self.store.write(f"alignment/{raw.key}.json", alignment.model_dump())
        self.progress(
            "Chapter alignment", aligned=len(self.alignments), total_chapters=len(self.pairs)
        )

    def occurrences(self, source):
        occurrences = []
        for raw, vp in self.pairs:
            if source in raw.title:
                occurrences.append(
                    {
                        **chapter_identity(raw, "chapter"),
                        "paragraph_ids": [-1],
                        "count": raw.title.count(source),
                        "raw": [raw.title],
                        "vp": [vp.title],
                    }
                )
            for group in self.alignments[raw.key].groups:
                matched = [i for i in group.raw if source in raw.paragraphs[i]]
                if matched:
                    occurrences.append(
                        {
                            **chapter_identity(raw, "chapter"),
                            "paragraph_ids": matched,
                            "count": sum(raw.paragraphs[i].count(source) for i in matched),
                            "raw": [raw.paragraphs[i] for i in group.raw],
                            "vp": [vp.paragraphs[i] for i in group.vp],
                        }
                    )
        return occurrences

    def known_source(self, source):
        return any(
            source in [term.source, *term.aliases, *term.forms] for term in self.terms.values()
        )

    def source_issue(self, source, inherited=None):
        problem = source_problem(source, self.terms)
        if problem:
            return problem
        # An absent inherited title may contain a numeral (e.g. a book called
        # "3个愿望"). Vet its stored evidence semantically rather than discard it
        # merely because this batch cannot repeat its original naming context.
        if (
            inherited
            and source in [inherited.source, *inherited.aliases, *inherited.forms]
            and not self.occurrences(source)
        ):
            return None
        return quantity_source_problem(
            source, (text for raw, _ in self.pairs for text in [raw.title, *raw.paragraphs])
        )

    async def resolve(self, source, inherited=None):
        issue = self.source_issue(source, inherited)
        if issue:
            self.store.write(
                f"term-audit/{digest(source)}.json",
                {"application_decision": "REJECT", "reason": issue},
            )
            return
        occurrences = self.occurrences(source)
        occurrence_pages = list(pages(occurrences))
        evidence = [""] * len(occurrence_pages)

        async def collect(item):
            index, page = item
            result = await self.ai(
                f"term:{source}:evidence:{index}",
                prompts.EVIDENCE,
                {"source": source, "occurrences": page},
                Evidence,
            )
            evidence[index] = result.findings

        # Evidence pages are independent. Use the same bounded worker pool and
        # provider gate, then retain source order for deterministic resolution.
        await self.parallel(enumerate(occurrence_pages), collect)
        # Hierarchical reduction visits every occurrence without a million-character request.
        while sum(map(len, evidence)) > 18000:
            summary_pages = list(pages(evidence, 12000))
            reduced = [""] * len(summary_pages)

            async def aggregate(item):
                index, page = item
                result = await self.ai(
                    f"term:{source}:aggregate",
                    prompts.EVIDENCE,
                    {"source": source, "all_evidence_summaries": page},
                    Evidence,
                )
                if len(result.findings) > 3000:
                    raise QualityError("Evidence summary exceeds compact context budget")
                reduced[index] = result.findings

            await self.parallel(enumerate(summary_pages), aggregate)
            evidence = reduced
        related = [
            t.model_dump()
            for t in self.terms.values()
            if source in [t.source, *t.aliases, *t.forms]
            or any(t.source in finding for finding in evidence)
        ]
        payload = {
            "source": source,
            "all_occurrence_evidence": evidence,
            "inherited": inherited.model_dump() if inherited else None,
            "related_entities": related,
        }
        task = f"term:{source}:resolve"
        result = await self.ai(task, prompts.RESOLVE, payload, Resolution)
        for attempt in range(3):
            previous = dict(self.terms)
            try:
                self.apply_resolution(source, inherited, result.model_copy(deep=True))
                sanity(self.terms)
                return
            except (QualityError, ValueError) as exc:
                self.terms = previous
                self.store.write(
                    f"resolution-rejections/{digest(source)}-{attempt}.json",
                    {
                        "error": str(exc),
                        "response": result.model_dump(),
                    },
                )
                if attempt == 2:
                    self.fail(
                        task,
                        f"Term {source}: resolution still invalid after two targeted corrections: {exc}",
                    )
                names = [source]
                if result.term:
                    names += [result.term.source, *result.term.aliases, *result.term.forms]
                conflicts = [
                    term.model_dump()
                    for term in self.terms.values()
                    if any(name in [term.source, *term.aliases, *term.forms] for name in names)
                    or (result.term and term.translation == result.term.translation)
                ]
                result = await self.ai(
                    task + f":repair:{attempt}",
                    prompts.RESOLVE,
                    {
                        **payload,
                        "structural_error": str(exc),
                        "previous_resolution": result.model_dump(),
                        "conflicting_entities": conflicts,
                        "repair_instruction": "Correct this ONE term resolution from RAW evidence. A person alias/title belongs on a canonical type=character entity, preferably the attested full name when identity is established. Include the original candidate as an alias/address form with its appropriate Vietnamese wording. forms MUST map exact Chinese source keys to Vietnamese wording, never generic labels like title or nickname. Preserve the original candidate's title/honorific/name meaning in forms[source]. Preserve locked mappings, entity identity, type and gender. Do not invent or merge uncertain aliases; reject unsupported candidates. Correct eligibility and malformed metadata.",
                    },
                    Resolution,
                )

    def apply_resolution(self, source, inherited, result):
        self.store.write(f"resolution/{digest(source)}.json", result.model_dump())
        if result.decision == "REJECT" or result.term is None:
            return
        gates = result.eligibility.model_dump()
        failed_gates = [
            name for name, passed in gates.items() if not passed and name != "evidence_supports"
        ]
        if failed_gates:
            self.store.write(
                f"term-audit/{digest(source)}.json",
                {
                    "provider_decision": result.decision,
                    "application_decision": "REJECT",
                    "failed_eligibility_gates": failed_gates,
                    "reason": result.reason,
                },
            )
            return
        term = result.term
        if source_name(term.source) == source_name(source) and term.source != source:
            # A purely typographic normalization is a provable identity, unlike
            # shortening a Chinese entity name or guessing an alias from context.
            if source not in term.aliases:
                term.aliases.append(source)
        if result.decision == "REVIEW" or not result.eligibility.evidence_supports:
            term.status = "provisional"
        if term.source != source and source not in [*term.aliases, *term.forms]:
            raise QualityError(f"Resolver lost source term {source}")
        # Provider metadata can include Vietnamese display names as aliases. They are
        # not Chinese source keys: quarantine them rather than abort a valid term/batch.
        inherited_names = (
            [inherited.source, *inherited.aliases, *inherited.forms] if inherited else []
        )

        def attested(name):
            return not self.source_issue(name, inherited) and (
                name in inherited_names or bool(self.occurrences(name))
            )

        invalid_names = [name for name in [*term.aliases, *term.forms] if not attested(name)]
        if invalid_names:
            self.store.write(
                f"term-audit/{digest(source)}.json",
                {
                    "rejected_source_metadata": invalid_names,
                    "reason": "Alias/form is not an eligible attested Chinese source key",
                },
            )
            term.aliases = [name for name in term.aliases if attested(name)]
            term.forms = {name: value for name, value in term.forms.items() if attested(name)}
        if not attested(term.source):
            raise QualityError(f"Unattested canonical source {term.source}")
        if term.source != source and source not in [*term.aliases, *term.forms]:
            raise QualityError(f"Resolver lost attested source term {source}")
        if term.type == "character_alias":
            raise QualityError(
                "Character alias must attach to a canonical type=character entity using aliases/forms, not be a separate canonical alias record"
            )
        if (
            term.type == "character"
            and term.source != source
            and source_name(term.source) != source_name(source)
            and source not in term.forms
        ):
            raise QualityError(
                f"Missing explicit Chinese-keyed address form for {source}: forms must map {source!r} to its appropriate Vietnamese wording, preserving its title/honorific/name meaning. Generic keys like title are invalid."
            )
        problem = term_problem(term, self.terms)
        if problem:
            raise QualityError(f"Invalid resolved term {term.source}: {problem}")
        existing = self.terms.get(term.source)
        if inherited and inherited.source == term.source:
            # The semantic audit can reject a corrupt legacy term, but cannot rewrite a
            # mapping it accepts as valid and locked, or silently promote inherited state.
            if inherited.status == "locked":
                term.translation = inherited.translation
                term.type = inherited.type
                term.gender = inherited.gender
            term.aliases = sorted(set(inherited.aliases + term.aliases))
            term.forms = {**term.forms, **inherited.forms}
            term.status = inherited.status
        if existing:
            if existing.status == "locked":
                term.translation = existing.translation
                term.status = existing.status
                term.type = existing.type
                term.gender = existing.gender
                term.evidence = existing.evidence
            term.aliases = sorted(set(existing.aliases + term.aliases))
            term.forms = {**term.forms, **existing.forms}
        # The resolver may establish identity after a variant was already discovered.
        # Fold that canonical duplicate into aliases/forms, preserving trusted mappings.
        duplicates = []
        for name in sorted(set([*term.aliases, *term.forms])):
            duplicate = self.terms.get(name)
            if duplicate is None or name == term.source:
                continue
            if duplicate.status == "locked" and duplicate.type != term.type:
                raise QualityError(
                    f"Alias merge changes the established type for {name}: {duplicate.type} versus {term.type}. Preserve trusted entity classification and do not merge a generic role with a particular person."
                )
            if (
                duplicate.gender != "unknown"
                and term.gender != "unknown"
                and duplicate.gender != term.gender
            ):
                raise QualityError(f"Alias identity conflicts with established gender for {name}")
            expected = term.forms.get(name, term.translation)
            if duplicate.status == "locked" and duplicate.translation != expected:
                # Identity can be established after an earlier title/name was
                # locked. Preserve that exact source mapping as an address form;
                # a different canonical title must not rewrite a historical rank.
                self.store.write(
                    f"term-audit/locked-form-{digest(name)}.json",
                    {
                        "source": name,
                        "canonical": term.source,
                        "preserved_translation": duplicate.translation,
                        "rejected_translation": expected,
                    },
                )
                term.forms[name] = duplicate.translation
            duplicates.append(name)
            term.aliases = sorted(set(term.aliases + duplicate.aliases))
            term.forms = {**duplicate.forms, **term.forms}
        for name in duplicates:
            del self.terms[name]
        self.terms[term.source] = term

    async def discover(self, pair):
        raw, _ = pair
        collected = {}
        for index, chunk in enumerate(self.chunks[raw.key]):
            result = await self.ai(
                f"chapter:{raw.key}:scan:{index}",
                prompts.DISCOVER,
                {**chunk, "raw_title": raw.title, "vp_title": pair[1].title},
                Candidates,
            )
            for candidate in result.candidates:
                source = source_name(candidate.source)
                if (
                    not self.source_issue(source)
                    and candidate.evidence
                    and candidate.source in candidate.evidence
                    and any(
                        candidate.evidence in text
                        for text in [raw.title, *[p["text"] for p in chunk["raw"]]]
                    )
                ):
                    collected[source] = candidate.evidence
        self.missed.update(collected)
        self.store.write(f"candidates/{raw.key}.json", collected)
        self.progress("Terminology analysis", scanned_chapter=raw.key)

    def remember_missed(self, validation, raw):
        for candidate in validation.missed_terms:
            source = source_name(candidate.source)
            if (
                not self.known_source(source)
                and not self.source_issue(source)
                and candidate.evidence
                and candidate.source in candidate.evidence
                and candidate.evidence in raw
            ):
                self.missed[source] = candidate.evidence
                self.store.write("missed-terms.json", self.missed)

    async def resolve_missed(self):
        async with self.term_lock:
            for source in sorted(self.missed):
                if not self.known_source(source):
                    await self.resolve(source)
            sanity(self.terms)
            self.store.write("working-dictionary.json", export_dictionary(self.terms))

    async def validate_and_repair(self, task, payload, translation):
        ids = [p["id"] for p in payload["raw"]]
        if [s.id for s in translation.segments] != ids:
            self.fail(task, f"{task}: translation paragraph IDs do not match RAW")
        for attempt in range(3):
            validation = await self.ai(
                task + ":validate",
                prompts.VALIDATE,
                {
                    **{key: value for key, value in payload.items() if key != "vp"},
                    "translation": translation.model_dump(),
                },
                Validation,
            )
            # An AI validator may accept a fluent synonym for a canonical name.
            # Keep the check grounded in this segment's Chinese source and request
            # localized RAW-aware repair instead of replacing Vietnamese globally.
            validation = validation.model_copy(deep=True)
            segments = {s.id: s.text for s in translation.segments}
            source_segments = [*payload["raw"], {"id": -1, "text": payload.get("raw_title", "")}]
            for paragraph in source_segments:
                text = translation.title if paragraph["id"] == -1 else segments[paragraph["id"]]
                for source, expected in terminology_gaps(
                    payload.get("terminology", []), paragraph["text"], text
                ):
                    validation.issues.append(
                        Issue(
                            segment_id=paragraph["id"],
                            kind="terminology",
                            explanation=f"RAW explicitly contains {source}; use its canonical dictionary form {expected!r} at that occurrence. Preserve all other RAW meaning.",
                        )
                    )
            self.remember_missed(
                validation,
                payload.get("raw_title", "") + "\n" + "\n".join(p["text"] for p in payload["raw"]),
            )
            if not validation.issues:
                return translation
            self.store.write(
                f"validation-rejections/{digest(task)}-{attempt}.json",
                {
                    "task": task,
                    "attempt": attempt,
                    "issues": [issue.model_dump() for issue in validation.issues],
                    "translation": translation.model_dump(),
                },
            )
            affected = sorted({issue.segment_id for issue in validation.issues})
            if any(i not in ids and i != -1 for i in affected):
                self.fail(task, f"{task}: validator returned unknown segment IDs")
            if attempt == 2:
                self.fail(
                    task, f"{task}: quality validation still fails after two targeted repairs"
                )
            # Only requested paragraphs are editable; neighborhood provides continuity.
            selected = set(affected)
            neighbors = set(selected)
            for i in affected:
                if i in ids:
                    pos = ids.index(i)
                    neighbors.update(ids[max(0, pos - 1) : pos + 2])
            chapter_key = task.split(":")[1]
            vp_ids = {
                paragraph_id
                for group in self.alignments[chapter_key].groups
                if neighbors.intersection(group.raw)
                for paragraph_id in group.vp
            }
            repair_payload = {
                **payload,
                "repair_attempt": attempt + 1,
                "repair_feedback": "The previous repair did not pass validation; correct the remaining issues."
                if attempt
                else "Correct the localized validation issues.",
                "raw": [p for p in payload["raw"] if p["id"] in neighbors],
                "vp": [p for p in payload["vp"] if p["id"] in vp_ids],
                "translation": {
                    "title": translation.title,
                    "segments": [s.model_dump() for s in translation.segments if s.id in neighbors],
                },
                "affected_ids": affected,
                "issues": [i.model_dump() for i in validation.issues],
            }
            repaired = await self.ai(
                task + f":repair:{attempt}", prompts.REPAIR, repair_payload, Repair
            )
            if sorted(s.id for s in repaired.segments) != affected:
                self.fail(task, f"{task}: repair changed unexpected segments")
            patches = {s.id: s.text for s in repaired.segments}
            for segment in translation.segments:
                if segment.id in patches:
                    segment.text = patches[segment.id]
            if -1 in patches:
                translation.title = patches[-1]
        raise AssertionError("unreachable")

    async def chapter(self, pair):
        raw, vp = pair
        key = raw.key
        raw_text = "\n".join(raw.paragraphs)
        terms = relevant(self.terms, raw_text + raw.title)
        fingerprint = digest(terms)
        saved = self.store.read(f"chapters/{key}.json")
        whole = {
            "raw_title": raw.title,
            "vp_title": vp.title,
            "raw": [{"id": i, "text": p} for i, p in enumerate(raw.paragraphs)],
            "vp": [{"id": i, "text": p} for i, p in enumerate(vp.paragraphs)],
            "terminology": terms,
        }
        if saved and saved["dictionary_hash"] == fingerprint:
            checked = Translation.model_validate(saved["translation"])
            texts = {s.id: s.text for s in checked.segments}
            gaps = terminology_gaps(terms, raw.title, checked.title)
            for paragraph in whole["raw"]:
                gaps.extend(terminology_gaps(terms, paragraph["text"], texts[paragraph["id"]]))
            if not gaps:
                return
        if saved:
            # Terminology discovered later triggers source-aware repairs to existing prose.
            updated = await self.validate_and_repair(
                f"chapter:{key}:full", whole, Translation.model_validate(saved["translation"])
            )
            self.store.write(
                f"chapters/{key}.json",
                {
                    "dictionary_hash": fingerprint,
                    **chapter_identity(raw),
                    "translation": updated.model_dump(),
                },
            )
            return
        context = await self.ai(f"chapter:{key}:context", prompts.CONTEXT, whole, Context)
        segments, title = [], ""
        translation_chunks = make_chunks(self.alignments[key], raw, vp)
        for index, chunk in enumerate(translation_chunks):
            self.activity[key] = {"chunk": index + 1, "chunks": len(self.chunks[key])}
            self.progress("Translating", active_chapters=self.activity)
            payload = {
                "CHAPTER CONTEXT": {
                    "analysis": context.context,
                    "raw_title": raw.title,
                    "vp_title": vp.title,
                },
                "PREVIOUS CONTEXT": {
                    "instruction": "CONTEXT ONLY. Do not translate again or include in output.",
                    "paragraphs": [s.text for s in segments[-2:]],
                },
                "RAW TO TRANSLATE": chunk["raw"],
                "VIETPHRASE REFERENCE": chunk["vp"],
                "TERMINOLOGY": terms,
            }
            chunk_key = digest(payload)
            checkpoint = self.store.read(f"chunks/{key}-{index}.json")
            if checkpoint and checkpoint["input_hash"] == chunk_key:
                translated = Translation.model_validate(checkpoint["translation"])
            else:
                translated = await self.ai(
                    f"chapter:{key}:chunk:{index}:translate",
                    prompts.TRANSLATE,
                    payload,
                    Translation,
                )
                translated = await self.validate_and_repair(
                    f"chapter:{key}:chunk:{index}",
                    {
                        **chunk,
                        "raw_title": raw.title,
                        "context": context.context,
                        "previous_context": [s.text for s in segments[-2:]],
                        "terminology": terms,
                    },
                    translated,
                )
                self.store.write(
                    f"chunks/{key}-{index}.json",
                    {"input_hash": chunk_key, "translation": translated.model_dump()},
                )
            previous_terms = terms
            await self.resolve_missed()
            terms = relevant(self.terms, raw_text + raw.title)
            if terms != previous_terms:
                translated = await self.validate_and_repair(
                    f"chapter:{key}:chunk:{index}:terminology",
                    {
                        **chunk,
                        "raw_title": raw.title,
                        "context": context.context,
                        "terminology": terms,
                    },
                    translated,
                )
                self.store.write(
                    f"chunks/{key}-{index}.json",
                    {"input_hash": chunk_key, "translation": translated.model_dump()},
                )
            segments.extend(translated.segments)
            title = title or translated.title
        self.progress("Full chapter validation", chapter=key)
        whole["terminology"] = relevant(self.terms, raw_text + raw.title)
        merged = Translation(title=title, segments=segments)
        merged = await self.validate_and_repair(f"chapter:{key}:full", whole, merged)
        self.store.write(
            f"chapters/{key}.json",
            {
                "dictionary_hash": fingerprint,
                **chapter_identity(raw),
                "translation": merged.model_dump(),
            },
        )
        self.activity.pop(key, None)
        self.progress(
            "Translating",
            active_chapters=self.activity,
            completed_chapters=sum(
                self.store.read(f"chapters/{r.key}.json") is not None for r, _ in self.pairs
            ),
        )

    async def final_validation(self, pair):
        raw, vp = pair
        saved = self.store.read(f"chapters/{raw.key}.json")
        terms = relevant(self.terms, raw.title + "\n".join(raw.paragraphs))
        payload = {
            "scope": "Final batch consistency against the canonical Book Dictionary",
            "raw_title": raw.title,
            "vp_title": vp.title,
            "raw": [{"id": i, "text": p} for i, p in enumerate(raw.paragraphs)],
            "vp": [{"id": i, "text": p} for i, p in enumerate(vp.paragraphs)],
            "terminology": terms,
        }
        checked = await self.validate_and_repair(
            f"chapter:{raw.key}:batch", payload, Translation.model_validate(saved["translation"])
        )
        self.store.write(
            f"chapters/{raw.key}.json",
            {
                "dictionary_hash": digest(terms),
                **chapter_identity(raw),
                "translation": checked.model_dump(),
            },
        )

    async def run(self):
        inputs = self.store.read("inputs.json")
        self.progress("Parsing", error=None, error_detail=None, active_chapters={})
        self.pairs = validate_inputs(inputs["raw"], inputs["vietphrase"])
        self.store.write("parser-version.json", {"version": PARSER_VERSION})
        await self.parallel(self.pairs, self.align)
        inherited, problems = load_legacy(inputs.get("dictionary"))
        self.store.write("legacy-audit.json", problems)
        self.progress("Validating inherited dictionary")
        previous_dictionary = self.store.read("working-dictionary.json")
        if previous_dictionary is not None:
            restored, invalid = load_legacy(previous_dictionary)
            if invalid:
                raise ValueError("Invalid dictionary checkpoint; inspect internal legacy audit")
            self.terms = {term.source: term for term in restored}
            sanity(self.terms)
        # Vet even absent legacy terms before treating them as authoritative.
        for term in inherited:
            if term.source not in self.terms:
                await self.resolve(term.source, term)
        await self.parallel(self.pairs, self.discover)
        self.progress("Dictionary resolution", candidate_count=len(self.missed))
        for index, source in enumerate(sorted(self.missed)):
            self.progress("Dictionary resolution", current_term=source, resolved_candidates=index)
            # These inputs already contain every current-batch occurrence. A
            # restored decision or established address form has no new RAW
            # evidence to justify another resolution on every resume/chunk.
            if not self.known_source(source):
                await self.resolve(source, self.terms.get(source))
        self.progress(
            "Dictionary resolution", current_term=None, resolved_candidates=len(self.missed)
        )
        sanity(self.terms)
        self.store.write("working-dictionary.json", export_dictionary(self.terms))
        # Replay missed terms from a crash after a chunk checkpoint but before resolution.
        known_misses = self.store.read("missed-terms.json", {})
        self.missed = known_misses
        for round_number in range(4):
            before = digest(export_dictionary(self.terms))
            await self.parallel(self.pairs, self.chapter)
            self.store.write("missed-terms.json", self.missed)
            self.progress("Final batch validation", reconciliation_round=round_number + 1)
            await self.parallel(self.pairs, self.final_validation)
            for source in sorted(self.missed):
                if not self.known_source(source):
                    await self.resolve(source)
            sanity(self.terms)
            self.store.write("working-dictionary.json", export_dictionary(self.terms))
            if digest(export_dictionary(self.terms)) == before:
                break
            # Changed chapter dictionaries invalidate just affected chapters/chunks;
            # source-aware full validation rechecks all occurrences under one dictionary.
        else:
            raise QualityError(
                "Missed terminology did not stabilize after four passes; resume required"
            )
        self.progress("Saving outputs")
        chapters = []
        for raw, _ in self.pairs:
            saved = self.store.read(f"chapters/{raw.key}.json")
            chapters.append(
                {
                    **chapter_identity(raw),
                    "title": saved["translation"]["title"],
                    "text": "\n\n".join(s["text"] for s in saved["translation"]["segments"]),
                }
            )
        self.store.write("translated.json", {"chapters": chapters})
        self.store.write("dictionary.json", export_dictionary(self.terms))
        self.store.progress(
            "done",
            stage="Complete",
            completed_chapters=len(chapters),
            total_chapters=len(chapters),
            active_chapters={},
        )
