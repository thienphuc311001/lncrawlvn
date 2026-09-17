"""Batch-first terminology, sequential chapter chunks, quality repair and reconciliation."""

from __future__ import annotations

import asyncio

from . import prompts
from .dictionary import (
    export_dictionary,
    load_legacy,
    relevant,
    sanity,
    source_problem,
    term_problem,
)
from .models import (
    Alignment,
    Candidates,
    Context,
    Evidence,
    Repair,
    Resolution,
    Translation,
    Validation,
)
from .parsing import make_chunks, pair_chapters, parse_chapters, validate_alignment
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
            {"instruction": instruction, "payload": payload, "schema": schema.model_json_schema()}
        )
        self.cache_keys.setdefault(task, set()).add(key)
        cached = self.store.read(f"cache/{key}.json")
        if cached is not None:
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
            "raw": raw.model_dump(),
            "vietphrase": vp.model_dump(),
            "paragraph_ids": "zero based",
            "chapter_order_confirmed": True,
        }
        alignment = await self.ai(
            f"chapter:{raw.number}:alignment", prompts.ALIGN, payload, Alignment
        )
        try:
            validate_alignment(alignment, raw, vp)
        except ValueError as exc:
            self.fail(f"chapter:{raw.number}:alignment", str(exc))
        self.alignments[raw.number] = alignment
        self.chunks[raw.number] = make_chunks(alignment, raw, vp)
        self.store.write(f"alignment/{raw.number}.json", alignment.model_dump())
        self.progress(
            "Chapter alignment", aligned=len(self.alignments), total_chapters=len(self.pairs)
        )

    def occurrences(self, source):
        occurrences = []
        for raw, vp in self.pairs:
            if source in raw.title:
                occurrences.append(
                    {
                        "chapter": raw.number,
                        "paragraph_ids": [-1],
                        "count": raw.title.count(source),
                        "raw": [raw.title],
                        "vp": [vp.title],
                    }
                )
            for group in self.alignments[raw.number].groups:
                matched = [i for i in group.raw if source in raw.paragraphs[i]]
                if matched:
                    occurrences.append(
                        {
                            "chapter": raw.number,
                            "paragraph_ids": matched,
                            "count": sum(raw.paragraphs[i].count(source) for i in matched),
                            "raw": [raw.paragraphs[i] for i in group.raw],
                            "vp": [vp.paragraphs[i] for i in group.vp],
                        }
                    )
        return occurrences

    async def resolve(self, source, inherited=None):
        if source_problem(source, self.terms):
            return
        occurrences = self.occurrences(source)
        evidence = []
        for index, page in enumerate(pages(occurrences)):
            result = await self.ai(
                f"term:{source}:evidence:{index}",
                prompts.EVIDENCE,
                {"source": source, "occurrences": page},
                Evidence,
            )
            evidence.append(result.findings)
        # Hierarchical reduction visits every occurrence without a million-character request.
        while sum(map(len, evidence)) > 18000:
            reduced = []
            for page in pages(evidence, 12000):
                result = await self.ai(
                    f"term:{source}:aggregate",
                    prompts.EVIDENCE,
                    {"source": source, "all_evidence_summaries": page},
                    Evidence,
                )
                if len(result.findings) > 3000:
                    raise QualityError("Evidence summary exceeds compact context budget")
                reduced.append(result.findings)
            evidence = reduced
        related = [
            t.model_dump()
            for t in self.terms.values()
            if source in [t.source, *t.aliases, *t.forms]
            or any(t.source in finding for finding in evidence)
        ]
        result = await self.ai(
            f"term:{source}:resolve",
            prompts.RESOLVE,
            {
                "source": source,
                "all_occurrence_evidence": evidence,
                "inherited": inherited.model_dump() if inherited else None,
                "related_entities": related,
            },
            Resolution,
        )
        self.store.write(f"resolution/{digest(source)}.json", result.model_dump())
        if result.decision == "REJECT" or result.term is None:
            return
        term = result.term
        if result.decision == "REVIEW":
            term.status = "provisional"
        if term.source != source and source not in [*term.aliases, *term.forms]:
            raise QualityError(f"Resolver lost source term {source}")
        # New source/aliases must be attested, never accepted from fabricated evidence.
        for name in [term.source, *term.aliases, *term.forms]:
            inherited_names = (
                [inherited.source, *inherited.aliases, *inherited.forms] if inherited else []
            )
            if name not in inherited_names and not self.occurrences(name):
                raise QualityError(f"Unattested source/alias {name}")
        if term_problem(term, self.terms):
            raise QualityError(f"Invalid resolved term: {term.source}")
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
            term.aliases = sorted(set(existing.aliases + term.aliases))
            term.forms = {**term.forms, **existing.forms}
        # The resolver may establish identity after a variant was already discovered.
        # Fold that canonical duplicate into aliases/forms, preserving trusted mappings.
        duplicates = []
        for name in [*term.aliases, *term.forms]:
            duplicate = self.terms.get(name)
            if duplicate is None or name == term.source:
                continue
            expected = term.forms.get(name, term.translation)
            if duplicate.status == "locked" and duplicate.translation != expected:
                raise QualityError(f"Alias merge conflicts with locked mapping {name}")
            duplicates.append(name)
            term.aliases = sorted(set(term.aliases + duplicate.aliases))
            term.forms = {**duplicate.forms, **term.forms}
        for name in duplicates:
            del self.terms[name]
        self.terms[term.source] = term

    async def discover(self, pair):
        raw, _ = pair
        collected = {}
        for index, chunk in enumerate(self.chunks[raw.number]):
            result = await self.ai(
                f"chapter:{raw.number}:scan:{index}",
                prompts.DISCOVER,
                {**chunk, "raw_title": raw.title, "vp_title": pair[1].title},
                Candidates,
            )
            for candidate in result.candidates:
                if (
                    not source_problem(candidate.source, self.terms)
                    and candidate.evidence
                    and candidate.source in candidate.evidence
                    and any(
                        candidate.evidence in text
                        for text in [raw.title, *[p["text"] for p in chunk["raw"]]]
                    )
                ):
                    collected[candidate.source] = candidate.evidence
        self.missed.update(collected)
        self.store.write(f"candidates/{raw.number}.json", collected)
        self.progress("Terminology analysis", scanned_chapter=raw.number)

    def remember_missed(self, validation, raw):
        for candidate in validation.missed_terms:
            if (
                candidate.source not in self.terms
                and not source_problem(candidate.source, self.terms)
                and candidate.evidence
                and candidate.source in candidate.evidence
                and candidate.evidence in raw
            ):
                self.missed[candidate.source] = candidate.evidence
                self.store.write("missed-terms.json", self.missed)

    async def resolve_missed(self):
        async with self.term_lock:
            for source in sorted(self.missed):
                if source not in self.terms:
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
                {**payload, "translation": translation.model_dump()},
                Validation,
            )
            self.remember_missed(
                validation,
                payload.get("raw_title", "") + "\n" + "\n".join(p["text"] for p in payload["raw"]),
            )
            if not validation.issues:
                return translation
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
            chapter_number = int(task.split(":")[1])
            vp_ids = {
                paragraph_id
                for group in self.alignments[chapter_number].groups
                if neighbors.intersection(group.raw)
                for paragraph_id in group.vp
            }
            repair_payload = {
                **payload,
                "raw": [p for p in payload["raw"] if p["id"] in neighbors],
                "vp": [p for p in payload["vp"] if p["id"] in vp_ids],
                "translation": {
                    "title": translation.title,
                    "segments": [s.model_dump() for s in translation.segments if s.id in neighbors],
                },
                "affected_ids": affected,
                "issues": [i.model_dump() for i in validation.issues],
            }
            repaired = await self.ai(task + ":repair", prompts.REPAIR, repair_payload, Repair)
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
        number = raw.number
        raw_text = "\n".join(raw.paragraphs)
        terms = relevant(self.terms, raw_text + raw.title)
        fingerprint = digest(terms)
        saved = self.store.read(f"chapters/{number}.json")
        if saved and saved["dictionary_hash"] == fingerprint:
            return
        whole = {
            "raw_title": raw.title,
            "vp_title": vp.title,
            "raw": [{"id": i, "text": p} for i, p in enumerate(raw.paragraphs)],
            "vp": [{"id": i, "text": p} for i, p in enumerate(vp.paragraphs)],
            "terminology": terms,
        }
        if saved:
            # Terminology discovered later triggers source-aware repairs to existing prose.
            updated = await self.validate_and_repair(
                f"chapter:{number}:full", whole, Translation.model_validate(saved["translation"])
            )
            self.store.write(
                f"chapters/{number}.json",
                {
                    "dictionary_hash": fingerprint,
                    "number": number,
                    "translation": updated.model_dump(),
                },
            )
            return
        context = await self.ai(f"chapter:{number}:context", prompts.CONTEXT, whole, Context)
        segments, title = [], ""
        translation_chunks = make_chunks(self.alignments[number], raw, vp)
        for index, chunk in enumerate(translation_chunks):
            self.activity[str(number)] = {"chunk": index + 1, "chunks": len(self.chunks[number])}
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
            checkpoint = self.store.read(f"chunks/{number}-{index}.json")
            if checkpoint and checkpoint["input_hash"] == chunk_key:
                translated = Translation.model_validate(checkpoint["translation"])
            else:
                translated = await self.ai(
                    f"chapter:{number}:chunk:{index}:translate",
                    prompts.TRANSLATE,
                    payload,
                    Translation,
                )
                translated = await self.validate_and_repair(
                    f"chapter:{number}:chunk:{index}",
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
                    f"chunks/{number}-{index}.json",
                    {"input_hash": chunk_key, "translation": translated.model_dump()},
                )
            previous_terms = terms
            await self.resolve_missed()
            terms = relevant(self.terms, raw_text + raw.title)
            if terms != previous_terms:
                translated = await self.validate_and_repair(
                    f"chapter:{number}:chunk:{index}:terminology",
                    {
                        **chunk,
                        "raw_title": raw.title,
                        "context": context.context,
                        "terminology": terms,
                    },
                    translated,
                )
                self.store.write(
                    f"chunks/{number}-{index}.json",
                    {"input_hash": chunk_key, "translation": translated.model_dump()},
                )
            segments.extend(translated.segments)
            title = title or translated.title
        self.progress("Full chapter validation", chapter=number)
        whole["terminology"] = relevant(self.terms, raw_text + raw.title)
        merged = Translation(title=title, segments=segments)
        merged = await self.validate_and_repair(f"chapter:{number}:full", whole, merged)
        self.store.write(
            f"chapters/{number}.json",
            {"dictionary_hash": fingerprint, "number": number, "translation": merged.model_dump()},
        )
        self.activity.pop(str(number), None)
        self.progress(
            "Translating",
            active_chapters=self.activity,
            completed_chapters=sum(
                self.store.read(f"chapters/{r.number}.json") is not None for r, _ in self.pairs
            ),
        )

    async def final_validation(self, pair):
        raw, vp = pair
        saved = self.store.read(f"chapters/{raw.number}.json")
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
            f"chapter:{raw.number}:batch", payload, Translation.model_validate(saved["translation"])
        )
        self.store.write(
            f"chapters/{raw.number}.json",
            {
                "dictionary_hash": digest(terms),
                "number": raw.number,
                "translation": checked.model_dump(),
            },
        )

    async def run(self):
        inputs = self.store.read("inputs.json")
        self.progress("Parsing", error=None, active_chapters={})
        self.pairs = pair_chapters(
            parse_chapters(inputs["raw"]), parse_chapters(inputs["vietphrase"])
        )
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
        for source in sorted(self.missed):
            if source not in self.terms or self.terms[source].status == "provisional":
                await self.resolve(source, self.terms.get(source))
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
                if source not in self.terms:
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
            saved = self.store.read(f"chapters/{raw.number}.json")
            chapters.append(
                {
                    "number": raw.number,
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
