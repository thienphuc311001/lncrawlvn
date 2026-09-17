"""Local whole-batch preprocessing, frozen terminology, translation and targeted repair."""

from __future__ import annotations

import asyncio
from collections import Counter
from types import MappingProxyType

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
)
from .models import (
    MODELS,
    PARSER_VERSION,
    PIPELINE_VERSION,
    Alignment,
    BatchResolution,
    Chapter,
    Repair,
    Resolution,
    Segment,
    Term,
    Translation,
)
from .parsing import deterministic_alignment, make_chunks, validate_inputs
from .preprocessing import batches, build_index, local_resolution, normalized
from .store import digest, pipeline_identity
from .validation import local_findings


class QualityError(RuntimeError):
    pass


def chapter_identity(chapter, field="number"):
    identity = {field: chapter.number}
    if chapter.volume is not None:
        identity["volume"] = chapter.volume
    return identity


class Pipeline:
    def __init__(self, store, scheduler):
        self.store, self.scheduler = store, scheduler
        self.terms, self.pairs, self.alignments, self.chunks = {}, [], {}, {}
        self.index, self.activity, self.cache_keys, self.decisions = {}, {}, {}, {}
        self.frozen_hash = None
        self.input_hash = None

    def progress(self, stage, **fields):
        self.store.progress(
            stage=stage, request_statistics=self.store.request_statistics(), **fields
        )

    def local(self, operation, message):
        self.store.log(
            {
                "status": "local_success",
                "operation": operation,
                "ai_requests": 0,
                "message": message,
            }
        )

    async def ai(
        self,
        task,
        instruction,
        payload,
        schema,
        operation="terminology_resolver",
        reason="Unresolved semantic terminology",
    ):
        key = digest(
            {
                "task": task,
                "instruction": instruction,
                "payload": payload,
                "schema": schema.model_json_schema(),
                "pipeline_version": PIPELINE_VERSION,
                "models": MODELS,
            }
        )
        self.cache_keys.setdefault(task, set()).add(key)
        cached = self.store.read(f"cache/{key}.json")
        if cached is not None:
            self.store.account(operation, "cache_hits")
            self.store.diagnostic(
                {
                    "task": task,
                    "operation": operation,
                    "status": "cached",
                    "message": "Reused completed checkpoint; no API request",
                }
            )
            return schema.model_validate(cached)
        self.store.account(operation, "logical")
        previous_model = None

        def record(meta):
            nonlocal previous_model
            if meta["status"] == "running":
                fallback = (
                    meta["model"] != previous_model
                    if previous_model
                    else meta["model"] != MODELS[0]
                )
                self.store.account(operation, "running", model_fallback=fallback)
                if meta.get("retry_count", 0):
                    self.store.account(operation, "retry")
                previous_model = meta["model"]
            self.store.diagnostic({"task": task, "operation": operation, "reason": reason, **meta})

        result = await self.scheduler.request(instruction, payload, schema, record)
        self.store.write(f"cache/{key}.json", result.model_dump())
        return result

    def fail(self, task, message):
        for operation, keys in self.cache_keys.items():
            if operation.startswith(task):
                for key in keys:
                    (self.store.path / f"cache/{key}.json").unlink(missing_ok=True)
        raise QualityError(message)

    async def parallel(self, items, function):
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
        saved = self.store.read(f"alignment/{raw.key}.json")
        alignment = Alignment.model_validate(saved) if saved else deterministic_alignment(raw, vp)
        # Check cached structural provenance, not merely paragraph ID coverage.
        proved = deterministic_alignment(raw, vp)
        if alignment != proved:
            raise QualityError(f"Invalid structural alignment checkpoint for {raw.key}")
        self.alignments[raw.key] = alignment
        self.chunks[raw.key] = make_chunks(alignment, raw, vp)
        if not saved:
            self.store.write(f"alignment/{raw.key}.json", alignment.model_dump())
        self.progress(
            "Local chapter alignment", aligned=len(self.alignments), total_chapters=len(self.pairs)
        )

    def occurrences(self, source):
        result = []
        for raw, vp in self.pairs:
            if source in raw.title:
                result.append(
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
                    result.append(
                        {
                            **chapter_identity(raw, "chapter"),
                            "paragraph_ids": matched,
                            "count": sum(raw.paragraphs[i].count(source) for i in matched),
                            "raw": [raw.paragraphs[i] for i in group.raw],
                            "vp": [vp.paragraphs[i] for i in group.vp],
                        }
                    )
        return result

    def known_source(self, source):
        return any(
            source in [term.source, *term.aliases, *term.forms] for term in self.terms.values()
        )

    def source_issue(self, source, inherited=None):
        problem = source_problem(source, self.terms)
        if problem:
            return problem
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
        """Diagnostic single-candidate resolution; production preprocessing batches candidates."""
        if self.frozen_hash is not None:
            raise QualityError("Dictionary is frozen; rebuild the batch to resolve new terminology")
        issue = self.source_issue(source, inherited)
        if issue:
            self.store.write(
                f"term-audit/{digest(source)}.json",
                {"application_decision": "REJECT", "reason": issue},
            )
            return
        payload = {
            "source": source,
            "representative_evidence": self.occurrences(source)[:6],
            "inherited": inherited.model_dump() if inherited else None,
            "related_entities": [term.model_dump() for term in self.terms.values()],
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
        if self.frozen_hash is not None:
            raise QualityError("Cannot mutate the frozen batch dictionary")
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

    def dictionary_conflicts(self):
        owners, translations, conflicts = {}, {}, set()
        for source, term in self.terms.items():
            translation = normalized(term.translation)
            if translation in translations:
                conflicts.update([source, translations[translation]])
            translations[translation] = source
            for name in [source, *term.aliases, *term.forms]:
                if name in owners and owners[name] != source:
                    conflicts.update([source, owners[name]])
                owners[name] = source
        return conflicts

    def resolver_payload(self, source, feedback=None):
        candidate = self.index["candidates"].get(source, {})
        inherited = self.terms.get(source)
        variants = candidate.get("vietphrase_variants", {})
        related = [
            term.model_dump()
            for term in self.terms.values()
            if term.source == source
            or source in [*term.aliases, *term.forms]
            or term.source[0] == source[0]
            or normalized(term.translation) in {normalized(value) for value in variants}
        ]
        return {
            "source": source,
            "frequency": candidate.get("frequency", 0),
            "signals": candidate.get("reasons", []),
            "vietphrase_consensus": variants,
            "representative_evidence": candidate.get("representative_evidence", []),
            "inherited": inherited.model_dump() if inherited else None,
            "related_entities": related[:12],
            "validator_feedback": feedback,
        }

    def save_decisions(self):
        self.store.write(
            "resolved-terms.json",
            {
                "input_hash": self.input_hash,
                "terms": [term.model_dump() for term in self.terms.values()],
                "decisions": self.decisions,
            },
        )

    async def resolve_candidates(self, sources, operation):
        pending, feedback = list(sources), {}
        for attempt in range(3):
            if not pending:
                return
            failed = []
            items = [self.resolver_payload(source, feedback.get(source)) for source in pending]
            for page in batches(items):
                page_sources = [item["source"] for item in page]
                task = f"batch:{operation}:{digest(page_sources)}:{attempt}"
                result = await self.ai(
                    task,
                    prompts.BATCH_RESOLVE,
                    {"candidates": page},
                    BatchResolution,
                    operation=operation,
                    reason="Ambiguous whole-novel terminology"
                    if operation == "terminology_resolver"
                    else "Concrete conflicting canonical mappings/aliases",
                )
                counts = Counter(item.source for item in result.results)
                by_source = {
                    item.source: item for item in result.results if counts[item.source] == 1
                }
                for source in page_sources:
                    item = by_source.get(source)
                    previous = {
                        key: value.model_copy(deep=True) for key, value in self.terms.items()
                    }
                    try:
                        if item is None:
                            raise QualityError(
                                "Missing, duplicated or malformed independent candidate result"
                            )
                        inherited = self.terms.get(source)
                        if item.decision == "REJECT":
                            self.terms.pop(source, None)
                        self.apply_resolution(
                            source,
                            inherited,
                            Resolution.model_validate(item.model_dump(exclude={"source"})),
                        )
                        # Only this candidate's namespace is checked here: unrelated
                        # pending semantic conflicts cannot invalidate independent work.
                        term = next(
                            (
                                value
                                for value in self.terms.values()
                                if source in [value.source, *value.aliases, *value.forms]
                            ),
                            None,
                        )
                        if term:
                            names = {term.source, *term.aliases, *term.forms}
                            connected = {
                                key: value
                                for key, value in self.terms.items()
                                if names.intersection([value.source, *value.aliases, *value.forms])
                                or normalized(value.translation) == normalized(term.translation)
                            }
                            sanity(connected)
                        self.decisions[source] = item.decision
                        self.save_decisions()
                    except (QualityError, ValueError) as exc:
                        self.terms = previous
                        feedback[source] = str(exc)
                        failed.append(source)
                        self.store.write(
                            f"resolution-rejections/{digest(source)}-{attempt}.json",
                            {"error": str(exc), "response": item.model_dump() if item else None},
                        )
                self.progress(
                    "Dictionary resolution",
                    resolved_candidates=len(self.decisions),
                    current_term=None,
                )
            pending = failed
        if pending:
            raise QualityError(
                f"Unresolved semantic dictionary candidates after two targeted batch corrections: {', '.join(pending[:20])}"
            )

    async def prepare_dictionary(self, inputs):
        inherited, problems = load_legacy(inputs.get("dictionary"))
        self.store.write("legacy-audit.json", problems)
        saved_index = self.store.read("terminology-index.json")
        if saved_index and saved_index.get("input_hash") == self.input_hash:
            self.index = saved_index["index"]
        else:
            self.progress("Local whole-novel terminology scan")
            self.index = await asyncio.to_thread(
                build_index, self.pairs, self.alignments, inherited
            )
            self.store.write(
                "terminology-index.json", {"input_hash": self.input_hash, "index": self.index}
            )
        for operation in ("scanning", "occurrence_aggregation", "evidence_selection"):
            self.local(
                operation,
                f"{len(self.index['candidates'])} whole-novel candidates; zero AI requests",
            )
        saved = self.store.read("resolved-terms.json")
        if saved and saved.get("input_hash") == self.input_hash:
            self.terms = {item["source"]: Term.model_validate(item) for item in saved["terms"]}
            self.decisions = saved["decisions"]
        else:
            self.terms = {term.source: term for term in inherited}
        conflicts = self.dictionary_conflicts()
        conflicts.update(term.source for term in inherited if "Legacy conflict:" in term.evidence)
        pending = []
        for source, candidate in self.index["candidates"].items():
            if source in self.decisions:
                continue
            if self.known_source(source) and source not in conflicts:
                self.decisions[source] = "REUSED"
                continue
            local_term = local_resolution(candidate)
            if (
                local_term
                and source not in conflicts
                and not any(
                    normalized(local_term.translation) == normalized(term.translation)
                    for term in self.terms.values()
                )
            ):
                self.terms[source] = local_term
                self.decisions[source] = "LOCAL_PROVISIONAL"
            elif candidate["frequency"] or source in conflicts:
                pending.append(source)
        self.save_decisions()
        self.progress(
            "Dictionary resolution",
            candidate_count=len(self.index["candidates"]),
            resolved_candidates=len(self.decisions),
        )
        semantic = sorted(set(pending).intersection(conflicts))
        ordinary = [source for source in pending if source not in conflicts]
        if semantic:
            await self.resolve_candidates(semantic, "semantic_dictionary_conflict")
        if ordinary:
            await self.resolve_candidates(ordinary, "terminology_resolver")
        # Namespace collisions are semantic only if structurally valid records remain.
        for round_number in range(2):
            conflicts = self.dictionary_conflicts()
            if not conflicts:
                break
            await self.resolve_candidates(sorted(conflicts), "semantic_dictionary_conflict")
        sanity(self.terms)
        audit = []
        for term in self.terms.values():
            problem = term_problem(term, self.terms)
            if problem:
                raise QualityError(f"Local dictionary audit: {term.source}: {problem}")
            for name in [term.source, *term.aliases, *term.forms]:
                if not any(name in unit["raw"] for unit in self.index["units"]):
                    audit.append(
                        {
                            "source": name,
                            "classification": "absent_from_current_raw",
                            "disposition": "retained_valid_cumulative_mapping",
                        }
                    )
        self.store.write("dictionary-audit.json", audit)
        self.local(
            "structural_dictionary_audit",
            "Schema, namespace, Unicode, mappings and RAW occurrence audit passed",
        )
        dictionary = export_dictionary(self.terms)
        self.store.write("working-dictionary.json", dictionary)
        self.freeze(dictionary)
        self.store.write(
            "frozen-dictionary.json",
            {
                "input_hash": self.input_hash,
                "dictionary_hash": self.frozen_hash,
                "dictionary": dictionary,
            },
        )

    def freeze(self, dictionary):
        terms, invalid = load_legacy(dictionary)
        if invalid:
            raise QualityError("Malformed frozen dictionary checkpoint")
        sanity({term.source: term for term in terms})
        self.terms = MappingProxyType({term.source: term.model_copy(deep=True) for term in terms})
        self.frozen_hash = digest(export_dictionary(self.terms))
        self.progress("Dictionary frozen", dictionary_hash=self.frozen_hash, dictionary_frozen=True)
        self.local("dictionary_freeze", f"All chapters use frozen dictionary {self.frozen_hash}")

    def assert_frozen(self):
        if self.frozen_hash is None or digest(export_dictionary(self.terms)) != self.frozen_hash:
            raise QualityError("Frozen dictionary changed; stop and rebuild the batch")

    def check_unresolved(self, task, payload, translation):
        found = [
            candidate.model_dump()
            for candidate in translation.unresolved_terms
            if not self.known_source(source_name(candidate.source))
            and candidate.source in candidate.evidence
            and candidate.evidence
            in payload.get("raw_title", "") + "\n" + "\n".join(p["text"] for p in payload["raw"])
        ]
        if found:
            self.store.write(
                f"frozen-conflicts/{digest(task)}.json",
                {"dictionary_hash": self.frozen_hash, "candidates": found},
            )
            raise QualityError(
                "New unresolved terminology reported after dictionary freeze. Review frozen-conflicts and submit an updated inherited dictionary as a new batch; finalized chapters and dictionary remain unchanged."
            )

    async def validate_and_repair(self, task, payload, translation):
        ids = [paragraph["id"] for paragraph in payload["raw"]]
        self.check_unresolved(task, payload, translation)
        for attempt in range(3):
            try:
                issues = local_findings(payload, translation)
            except ValueError as exc:
                self.fail(task, str(exc))
            if not issues:
                self.local(
                    "normal_output_validation",
                    f"{task}: local checks passed; zero AI review requests",
                )
                return translation
            self.store.write(
                f"validation-rejections/{digest(task)}-{attempt}.json",
                {
                    "task": task,
                    "issues": [issue.model_dump() for issue in issues],
                    "translation": translation.model_dump(),
                },
            )
            if attempt == 2:
                self.fail(task, f"{task}: local validation still fails after two targeted repairs")
            affected = sorted({issue.segment_id for issue in issues})
            selected = set(affected)
            chapter_key = task.split(":")[1]
            vp_ids = {
                value
                for group in self.alignments[chapter_key].groups
                if selected.intersection(group.raw)
                for value in group.vp
            }
            neighbors = set()
            for paragraph_id in affected:
                if paragraph_id in ids:
                    index = ids.index(paragraph_id)
                    neighbors.update(ids[max(0, index - 1) : index + 2])
            repair_payload = {
                "raw_title": payload.get("raw_title", ""),
                "vp_title": payload.get("vp_title", ""),
                "raw": [paragraph for paragraph in payload["raw"] if paragraph["id"] in selected],
                "vp": [paragraph for paragraph in payload["vp"] if paragraph["id"] in vp_ids],
                "terminology": payload.get("terminology", []),
                "dictionary_hash": self.frozen_hash,
                "affected_ids": affected,
                "issues": [issue.model_dump() for issue in issues],
                "translation": {
                    "title": translation.title,
                    "segments": [s.model_dump() for s in translation.segments if s.id in selected],
                },
                "previous_context": payload.get("previous_context", []),
                "neighbor_context": [
                    s.model_dump()
                    for s in translation.segments
                    if s.id in neighbors and s.id not in selected
                ],
                "repair_attempt": attempt + 1,
                "repair_feedback": "The previous repair did not pass local validation; correct the remaining issues."
                if attempt
                else "Correct only the concrete local findings.",
            }
            repaired = await self.ai(
                task + f":repair:{attempt}",
                prompts.REPAIR,
                repair_payload,
                Repair,
                operation="repair",
                reason="Concrete failed local validator findings: "
                + ", ".join(sorted({issue.kind for issue in issues})),
            )
            if sorted(segment.id for segment in repaired.segments) != affected:
                self.fail(task, f"{task}: repair returned unexpected/missing paragraph IDs")
            patched = {segment.id: segment.text for segment in translation.segments}
            for segment in repaired.segments:
                if segment.id == -1:
                    translation.title = segment.text
                else:
                    patched[segment.id] = segment.text
            translation.segments = [
                Segment(id=paragraph_id, text=patched[paragraph_id]) for paragraph_id in ids
            ]
        raise AssertionError("unreachable")

    async def chapter(self, pair):
        self.assert_frozen()
        raw, vp = pair
        key = raw.key
        terms = relevant(self.terms, raw.title + "\n" + "\n".join(raw.paragraphs))
        whole = {
            "raw_title": raw.title,
            "vp_title": vp.title,
            "raw": [{"id": index, "text": text} for index, text in enumerate(raw.paragraphs)],
            "vp": [{"id": index, "text": text} for index, text in enumerate(vp.paragraphs)],
            "terminology": terms,
        }
        saved = self.store.read(f"chapters/{key}.json")
        if (
            saved
            and saved.get("pipeline_input_hash") == self.input_hash
            and saved["dictionary_hash"] == self.frozen_hash
        ):
            checked = await self.validate_and_repair(
                f"chapter:{key}:full", whole, Translation.model_validate(saved["translation"])
            )
            self.store.write(f"chapters/{key}.json", {**saved, "translation": checked.model_dump()})
            return
        segments, title = [], ""
        for index, chunk in enumerate(self.chunks[key]):
            self.assert_frozen()
            self.activity[key] = {"chunk": index + 1, "chunks": len(self.chunks[key])}
            self.progress("Translating", active_chapters=dict(self.activity))
            chunk_terms = relevant(
                self.terms, raw.title + "\n" + "\n".join(p["text"] for p in chunk["raw"])
            )
            previous = [segment.text[-800:] for segment in segments[-2:]]
            payload = {
                "CHAPTER CONTEXT": {
                    **chapter_identity(raw),
                    "raw_title": raw.title,
                    "vp_title": vp.title,
                },
                "PREVIOUS CONTEXT": {
                    "instruction": "CONTEXT ONLY. Do not translate again.",
                    "paragraphs": previous,
                },
                "RAW TO TRANSLATE": chunk["raw"],
                "VIETPHRASE REFERENCE": chunk["vp"],
                "TERMINOLOGY": chunk_terms,
                "dictionary_hash": self.frozen_hash,
            }
            chunk_key = digest({"payload": payload, "pipeline_input_hash": self.input_hash})
            checkpoint = self.store.read(f"chunks/{key}-{index}.json")
            if checkpoint and checkpoint["input_hash"] == chunk_key:
                translated = Translation.model_validate(checkpoint["translation"])
            else:
                translated = await self.ai(
                    f"chapter:{key}:chunk:{index}:translate",
                    prompts.TRANSLATE,
                    payload,
                    Translation,
                    operation="translation",
                    reason="Translate this RAW chunk using the already frozen whole-batch dictionary",
                )
            translated = await self.validate_and_repair(
                f"chapter:{key}:chunk:{index}",
                {
                    **chunk,
                    "raw_title": raw.title,
                    "vp_title": vp.title,
                    "previous_context": previous,
                    "terminology": chunk_terms,
                },
                translated,
            )
            self.store.write(
                f"chunks/{key}-{index}.json",
                {
                    "input_hash": chunk_key,
                    "dictionary_hash": self.frozen_hash,
                    "translation": translated.model_dump(),
                },
            )
            segments.extend(translated.segments)
            title = title or translated.title
        merged = await self.validate_and_repair(
            f"chapter:{key}:full", whole, Translation(title=title, segments=segments)
        )
        self.store.write(
            f"chapters/{key}.json",
            {
                "dictionary_hash": self.frozen_hash,
                "pipeline_input_hash": self.input_hash,
                **chapter_identity(raw),
                "translation": merged.model_dump(),
            },
        )
        self.activity.pop(key, None)
        self.progress(
            "Translating",
            active_chapters=dict(self.activity),
            completed_chapters=sum(
                self.store.read(f"chapters/{r.key}.json") is not None for r, _ in self.pairs
            ),
        )

    async def run(self):
        inputs = self.store.read("inputs.json")
        self.input_hash = pipeline_identity(inputs)
        self.progress(
            "Local parsing",
            error=None,
            error_detail=None,
            active_chapters={},
            dictionary_frozen=False,
        )
        parsed = self.store.read("parsed-chapters.json")
        if parsed and parsed["input_hash"] == self.input_hash:
            self.pairs = [
                (Chapter.model_validate(pair[0]), Chapter.model_validate(pair[1]))
                for pair in parsed["pairs"]
            ]
        else:
            self.pairs = validate_inputs(inputs["raw"], inputs["vietphrase"])
            self.store.write(
                "parsed-chapters.json",
                {
                    "input_hash": self.input_hash,
                    "pairs": [[r.model_dump(), v.model_dump()] for r, v in self.pairs],
                },
            )
        self.store.write("parser-version.json", {"version": PARSER_VERSION})
        await self.parallel(self.pairs, self.align)
        self.local("alignment", "Logical lines/blank blocks paired locally; zero AI requests")
        self.local(
            "chunk_construction", "Monotonic paragraph groups chunked locally; zero AI requests"
        )
        frozen = self.store.read("frozen-dictionary.json")
        if frozen and frozen["input_hash"] == self.input_hash:
            self.freeze(frozen["dictionary"])
            if self.frozen_hash != frozen["dictionary_hash"]:
                raise QualityError("Frozen dictionary hash mismatch")
        else:
            await self.prepare_dictionary(inputs)
        self.assert_frozen()
        await self.parallel(self.pairs, self.chapter)
        self.assert_frozen()
        self.progress("Local final batch validation")
        chapters = []
        for raw, _ in self.pairs:
            saved = self.store.read(f"chapters/{raw.key}.json")
            if saved["dictionary_hash"] != self.frozen_hash:
                raise QualityError(f"Chapter {raw.key} does not use the frozen batch dictionary")
            chapters.append(
                {
                    **chapter_identity(raw),
                    "title": saved["translation"]["title"],
                    "text": "\n\n".join(
                        segment["text"] for segment in saved["translation"]["segments"]
                    ),
                }
            )
        self.store.write("translated.json", {"chapters": chapters})
        self.store.write(
            "dictionary.json",
            {**export_dictionary(self.terms), "dictionary_hash": self.frozen_hash},
        )
        self.store.progress(
            "done",
            stage="Complete",
            completed_chapters=len(chapters),
            total_chapters=len(chapters),
            active_chapters={},
            dictionary_frozen=True,
            dictionary_hash=self.frozen_hash,
            request_statistics=self.store.request_statistics(),
        )
