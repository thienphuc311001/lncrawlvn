"""Deterministic RAW/VietPhrase translation pipeline.

The pipeline has one mutable phase (pre-freeze dictionary construction) and one
immutable phase (chapter translation). Terminology has only two runtime
decisions: CONFIRMED mappings are enforced, everything else is IGNORE metadata.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from types import MappingProxyType

from . import prompts, style
from .dictionary import (
    AUDIT_ONLY_CLASSIFICATIONS,
    clean_identity_forms,
    export_dictionary,
    load_legacy,
    normalize_term_register,
    quantity_source_problem,
    relevant,
    sanity,
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
    ResolutionPolicy,
    Segment,
    Term,
    Translation,
)
from .parsing import deterministic_alignment, make_chunks, validate_inputs
from .preprocessing import batches, build_index, local_resolution
from .store import digest, pipeline_identity
from .validation import local_findings


class QualityError(RuntimeError):
    pass


def chapter_identity(chapter, field="number"):
    result = {field: chapter.number}
    if chapter.volume is not None:
        result["volume"] = chapter.volume
    return result


class Pipeline:
    def __init__(self, store, scheduler):
        self.store, self.scheduler = store, scheduler
        self.terms = {}
        self.pairs = []
        self.alignments = {}
        self.chunks = {}
        self.index = {}
        self.activity = {}
        self.cache_keys = {}
        self.decisions = {}
        self.ignored = {}
        self.form_cleanup = []
        self._raw_contexts = None
        self.register_cleanup = []
        self.style_profile = {}
        self.address_register = style.SINO_VIETNAMESE
        self.address_register_source = "configured"
        self.outcomes = {}
        self.metrics = Counter()
        self.frozen_hash = None
        self.input_hash = None
        self.compatible_input_hashes = set()
        self.compatible_frozen_hashes = set()
        policy_data = self.store.read(
            "resolution-policy.json", ResolutionPolicy.from_environment().model_dump()
        )
        # Old checkpoints carried a resolver-correction limit. The active
        # architecture has no resolver repair loop, so discard that legacy
        # setting while keeping the checkpoint resumable.
        policy_data.pop("max_targeted_corrections", None)
        self.policy = ResolutionPolicy.model_validate(policy_data)

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
        reason="Translation pipeline operation",
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
                    "message": "Reused completed model response",
                }
            )
            return schema.model_validate(cached)

        self.store.account(operation, "logical")
        previous_model = None

        def record(metadata):
            nonlocal previous_model
            if metadata["status"] == "running":
                fallback = (
                    metadata["model"] != previous_model
                    if previous_model
                    else metadata["model"] != MODELS[0]
                )
                self.store.account(operation, "running", model_fallback=fallback)
                if metadata.get("retry_count", 0):
                    self.store.account(operation, "retry")
                previous_model = metadata["model"]
            self.store.diagnostic(
                {"task": task, "operation": operation, "reason": reason, **metadata}
            )

        result = await self.scheduler.request(instruction, payload, schema, record)
        self.store.write(f"cache/{key}.json", result.model_dump())
        return result

    def fail(self, task, message):
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
        proven = deterministic_alignment(raw, vp)
        if alignment != proven:
            raise QualityError(f"Invalid structural alignment checkpoint for {raw.key}")
        self.alignments[raw.key] = alignment
        self.chunks[raw.key] = make_chunks(alignment, raw, vp)
        if not saved:
            self.store.write(f"alignment/{raw.key}.json", alignment.model_dump())
        self.progress(
            "Local chapter alignment",
            aligned=len(self.alignments),
            total_chapters=len(self.pairs),
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
                matching = [index for index in group.raw if source in raw.paragraphs[index]]
                if matching:
                    result.append(
                        {
                            **chapter_identity(raw, "chapter"),
                            "paragraph_ids": matching,
                            "count": sum(raw.paragraphs[index].count(source) for index in matching),
                            "raw": [raw.paragraphs[index] for index in group.raw],
                            "vp": [vp.paragraphs[index] for index in group.vp],
                        }
                    )
        return result

    def known_source(self, source):
        return any(
            source in [term.source, *term.aliases, *term.forms]
            for term in self.terms.values()
        )

    def source_issue(self, source, inherited=None):
        problem = source_problem(source, self.terms)
        if problem:
            return problem
        if inherited and source in [inherited.source, *inherited.aliases, *inherited.forms]:
            return None
        return quantity_source_problem(
            source,
            (text for raw, _ in self.pairs for text in [raw.title, *raw.paragraphs]),
        )

    def resolver_payload(self, source):
        candidate = self.index.get("candidates", {}).get(source, {})
        inherited = next(
            (
                term
                for term in self.terms.values()
                if source in [term.source, *term.aliases, *term.forms]
            ),
            None,
        )
        payload = {
            "source": source,
            "frequency": candidate.get("frequency", 0),
            "signals": candidate.get("reasons", []),
            "vietphrase_evidence": candidate.get("vietphrase_variants", {}),
            "representative_evidence": candidate.get("representative_evidence", []),
            "inherited_confirmed": inherited.model_dump() if inherited else None,
        }
        # A title/kinship/address shape is not a character identity: tell the
        # resolver its class and the register it must render it in.
        address = style.address_payload(
            source,
            inherited.source if inherited else "",
            inherited.translation if inherited else "",
            self.address_register,
            self.evidence_contexts(source),
        )
        if address:
            payload["address_form"] = address
        return payload

    def evidence_contexts(self, source):
        """Bounded RAW windows already selected for one candidate (no rescan)."""
        candidate = self.index.get("candidates", {}).get(source, {})
        return [
            item.get("raw", "")
            for item in candidate.get("representative_evidence", [])
            if item.get("raw")
        ]

    def ignore(self, source, reason, **extra):
        record = {"source": source, "state": "IGNORE", "reason": reason, **extra}
        self.ignored[source] = record
        self.store.write(f"term-audit/{digest(source)}.json", record)

    def raw_contexts(self):
        # Called once per confirmed term by the register guard, so the whole
        # batch's paragraphs are collected at most once.
        if self._raw_contexts is None:
            self._raw_contexts = [
                text
                for raw, _ in self.pairs
                for text in [raw.title, *raw.paragraphs]
            ]
        return self._raw_contexts

    def record_form_cleanup(self, problems):
        cleanup = [
            problem
            for problem in problems
            if problem.get("classification") == "form_cleanup"
        ]
        if not cleanup:
            return
        self.form_cleanup.extend(cleanup)
        removed = sum(len(item.get("removed_forms", [])) for item in self.form_cleanup)
        preserved = sum(len(item.get("preserved_forms", [])) for item in self.form_cleanup)
        self.store.write("dictionary-form-cleanup.json", {"entries": self.form_cleanup})
        self.local(
            "dictionary_form_cleanup",
            f"Removed contextual pseudo-forms: {removed}; preserved identity forms: {preserved}",
        )

    def record_register_cleanup(self, problems):
        """Audit address/title forms that were normalized or dropped locally."""
        cleanup = [
            problem
            for problem in problems
            if problem.get("classification") == "register_form_cleanup"
        ]
        if not cleanup:
            return
        self.register_cleanup.extend(cleanup)
        normalized = sum(len(item.get("normalizations", [])) for item in cleanup)
        removed = sum(len(item.get("removed_forms", [])) for item in cleanup)
        self.store.write(
            "dictionary-register-cleanup.json", {"entries": self.register_cleanup}
        )
        self.local(
            "dictionary_register_cleanup",
            f"Register {self.address_register}: normalized forms={normalized}; removed forms={removed}",
        )

    def refresh_style(self):
        """Derive the batch address/title register from confirmed terminology."""
        stored = style.stored_register(
            self.store.read("frozen-dictionary.json"),
            self.terms.values(),
        )
        self.style_profile = style.profile(
            self.terms.values(),
            self.policy.min_register_evidence,
            self.policy.min_register_dominance,
        )
        if stored in (style.SINO_VIETNAMESE, style.MODERN):
            self.address_register, self.address_register_source = stored, "stored"
        else:
            self.address_register, self.address_register_source = style.effective_register(
                self.policy.address_register, self.style_profile
            )
        self.style_profile["register"] = self.address_register
        self.style_profile["source"] = self.address_register_source
        self.store.write("dictionary-style-profile.json", self.style_profile)
        return self.style_profile

    def apply_register_policy(self, term, reason):
        """Normalize or drop address forms that contradict the batch register."""
        normalizations, removed = normalize_term_register(
            term, self.address_register, self.evidence_contexts(term.source)
        )
        if not normalizations and not removed:
            return
        self.record_register_cleanup(
            [
                {
                    "classification": "register_form_cleanup",
                    "source": term.source,
                    "register": self.address_register,
                    "normalizations": normalizations,
                    "removed_forms": removed,
                    "preserved_forms": sorted(term.forms),
                    "reason": reason,
                }
            ]
        )

    def apply_resolution(self, source, inherited, result):
        """Install only a complete ACCEPT result; all uncertainty is IGNORE."""
        if self.frozen_hash is not None:
            raise QualityError("Cannot mutate the frozen batch dictionary")
        self.store.write(f"resolution/{digest(source)}.json", result.model_dump())
        if result.decision != "ACCEPT" or result.term is None:
            return None
        failed = [
            name for name, passed in result.eligibility.model_dump().items() if not passed
        ]
        if failed:
            self.ignore(source, "failed eligibility gates", failed_gates=failed)
            return None
        if result.term.type == "character_alias":
            raise QualityError("character_alias cannot be a canonical dictionary identity")
        try:
            term = Term.model_validate(result.term.model_dump())
        except (ValueError, TypeError) as exc:
            raise QualityError(f"Invalid resolver metadata: {exc}") from exc

        term.status = "locked"
        term.enforceable = True
        term.semantic_resolution = "resolved"
        term.needs_review = False
        term.resolution_reason = None
        term.runtime_state = "CONFIRMED"
        term.aliases = sorted(set(alias for alias in term.aliases if alias != term.source))
        term.forms.pop(term.source, None)
        if term.source != source and source not in [*term.aliases, *term.forms]:
            raise QualityError(f"Resolver lost source term {source}")
        # A surname-prefixed address form is a reference to a person, never a
        # person: 唐姐 must be attached to 唐菲菲, not created as its own
        # character.  Bare nicknames (老秦) and bare titles stay eligible
        # because they can be the only attested way a character is named.
        spec = style.address_spec(source)
        if spec and spec["surname"] and term.source == source:
            raise QualityError(
                f"address form {source} cannot be a canonical identity; "
                "attach it to the proven full name"
            )

        inherited_names = (
            [inherited.source, *inherited.aliases, *inherited.forms] if inherited else []
        )

        removed, preserved = clean_identity_forms(
            term,
            self.raw_contexts(),
            require_evidence=True,
        )
        if removed:
            self.record_form_cleanup(
                [
                    {
                        "classification": "form_cleanup",
                        "source": term.source,
                        "removed_forms": removed,
                        "preserved_forms": preserved,
                        "reason": "removed resolver-proposed contextual character pseudo-forms",
                    }
                ]
            )

        def attested(name):
            return not source_problem(name) and (
                name in inherited_names or bool(self.occurrences(name))
            )

        term.aliases = [alias for alias in term.aliases if attested(alias)]
        term.forms = {name: value for name, value in term.forms.items() if attested(name)}
        if not attested(term.source):
            raise QualityError(f"Unattested canonical source {term.source}")
        problem = term_problem(term, self.terms)
        if problem:
            raise QualityError(f"Invalid resolved term {term.source}: {problem}")

        owner = next(
            (
                existing
                for existing in self.terms.values()
                if source in [existing.source, *existing.aliases, *existing.forms]
            ),
            None,
        )
        existing = self.terms.get(term.source) or owner
        if existing:
            if existing.translation != term.translation:
                raise QualityError(f"confirmed identity {existing.source} has contradictory mappings")
            term.source = existing.source
            term.translation = existing.translation
            term.type = existing.type
            term.gender = existing.gender
            term.aliases = sorted(set(existing.aliases + term.aliases))
            term.forms = {**term.forms, **existing.forms}
            term.form_kinds = {**term.form_kinds, **existing.form_kinds}

        # Address/title forms must keep the batch register: rewrite them to the
        # deterministic established rendering, or drop the form and keep only
        # the canonical identity.
        self.apply_register_policy(
            term, "aligned resolver address/title forms with the batch register"
        )

        for name in [*term.aliases, *term.forms]:
            other = next(
                (
                    existing
                    for existing in self.terms.values()
                    if existing.source != term.source
                    and name in [existing.source, *existing.aliases, *existing.forms]
                ),
                None,
            )
            if other:
                raise QualityError(
                    f"source {name} already belongs to confirmed identity {other.source}"
                )
        self.terms[term.source] = term
        return term

    async def resolve(self, source, inherited=None):
        """Compatibility helper for one candidate; it still has one AI call."""
        if self.frozen_hash is not None:
            raise QualityError("Dictionary is frozen; rebuild the batch to resolve terminology")
        issue = self.source_issue(source, inherited)
        if issue:
            self.ignore(source, issue)
            return None
        result = await self.ai(
            f"term:{source}:resolve",
            prompts.RESOLVE,
            {
                "source": source,
                "representative_evidence": self.occurrences(source)[:6],
                "inherited_confirmed": inherited.model_dump() if inherited else None,
            },
            Resolution,
            reason="Ambiguous plausible terminology candidate",
        )
        try:
            term = self.apply_resolution(source, inherited, result)
        except (QualityError, ValueError) as exc:
            self.ignore(source, str(exc))
            return None
        if term is None:
            self.ignore(source, result.reason or "candidate not confirmed")
        return term

    def dictionary_conflicts(self):
        """Return concrete source/alias collisions; shared targets are legal."""
        owners, conflicts = {}, set()
        for source, term in self.terms.items():
            if not term.enforceable:
                continue
            for name in [source, *term.aliases, *term.forms]:
                owner = owners.get(name)
                if owner and owner != source:
                    conflicts.update((source, owner))
                else:
                    owners[name] = source
        return conflicts

    async def resolve_candidates(self, sources):
        """Resolve plausible candidates in bounded batches, without correction loops."""
        sources = list(dict.fromkeys(sources))
        if not sources:
            return
        for page in batches([self.resolver_payload(source) for source in sources]):
            page_sources = [item["source"] for item in page]
            result = await self.ai(
                f"batch:terminology:{digest(page_sources)}",
                prompts.BATCH_RESOLVE,
                {
                    "candidates": page,
                    "translation_style": style.resolver_context(
                        self.address_register,
                        self.address_register_source,
                        self.style_profile,
                    ),
                },
                BatchResolution,
                operation="terminology_resolver",
                reason="Ambiguous plausible terminology candidates",
            )
            by_source = {}
            for item in result.results:
                if item.source in page_sources and item.source not in by_source:
                    by_source[item.source] = item
            for source in page_sources:
                item = by_source.get(source)
                inherited = next(
                    (
                        term
                        for term in self.terms.values()
                        if source in [term.source, *term.aliases, *term.forms]
                    ),
                    None,
                )
                try:
                    if item is None:
                        raise QualityError("resolver omitted or malformed candidate")
                    term = self.apply_resolution(
                        source,
                        inherited,
                        Resolution.model_validate(item.model_dump(exclude={"source"})),
                    )
                except (QualityError, ValueError) as exc:
                    term = None
                    self.ignore(source, str(exc))
                if term is None:
                    self.decisions[source] = "IGNORE"
                    self.outcomes[source] = {
                        "source": source,
                        "state": "IGNORE",
                        "reason": item.reason if item else "resolver omitted or malformed candidate",
                    }
                else:
                    self.decisions[source] = "CONFIRMED"
                    self.outcomes[source] = {
                        "source": source,
                        "state": "CONFIRMED",
                        "translation": term.translation,
                        "reason": item.reason if item else "confirmed",
                    }
            self.save_decisions()
        self.progress(
            "Terminology resolution",
            resolver_candidates=len(sources),
            confirmed_terms=sum(value == "CONFIRMED" for value in self.decisions.values()),
            ignored_candidates=sum(value == "IGNORE" for value in self.decisions.values()),
        )

    def save_decisions(self):
        self.store.write(
            "resolved-terms.json",
            {
                "input_hash": self.input_hash,
                "terms": [term.model_dump() for term in self.terms.values()],
                "decisions": self.decisions,
                "outcomes": self.outcomes,
            },
        )

    def migrate_legacy_resolution(self, source):
        """Reuse a structurally valid old ACCEPT response without another call."""
        paths = sorted((self.store.path / "resolution-rejections").glob(f"{digest(source)}-*.json"))
        for path in reversed(paths):
            record = self.store.read(str(path.relative_to(self.store.path)))
            response = record.get("response") if record else None
            if not response:
                continue
            try:
                # Older resolver checkpoints wrapped the strict response with
                # a top-level source field.  Accept that envelope only during
                # migration, and never allow it to migrate to another term.
                legacy_source = response.get("source") if isinstance(response, dict) else None
                if legacy_source is not None and legacy_source != source:
                    continue
                normalized = {
                    key: value for key, value in response.items() if key != "source"
                }
                result = Resolution.model_validate(normalized)
                if result.decision != "ACCEPT" or result.term is None:
                    continue
                term = self.apply_resolution(source, None, result)
            except (QualityError, ValueError, TypeError):
                continue
            if term:
                self.decisions[source] = "CONFIRMED"
                self.outcomes[source] = {
                    "source": source,
                    "state": "CONFIRMED",
                    "translation": term.translation,
                    "reason": "migrated valid legacy resolver response",
                }
                return True
        return False

    def dictionary_report(self):
        entries = sorted(self.outcomes.values(), key=lambda item: item.get("source", ""))
        confirmed = len(self.terms)
        ignored = len(self.ignored)
        removed_forms = sum(
            len(item.get("removed_forms", [])) for item in self.form_cleanup
        )
        preserved_forms = sum(
            len(item.get("preserved_forms", [])) for item in self.form_cleanup
        )
        return {
            "summary": {
                "confirmed_terms": confirmed,
                "ignored_candidates": ignored,
                "locked_terms": confirmed,
                "provisional_terms": 0,
                "needs_review": 0,
                "report_only_terms": ignored,
                "unresolved_terms": 0,
                "unresolved_plausible_terms": 0,
                "rejected_generic_candidates": sum(
                    item.get("classification") in {
                        "generic_phrase",
                        "common_noun",
                        "verb_phrase",
                        "descriptive_phrase",
                    }
                    for item in self.index.get("report_only", {}).values()
                ),
                "fatal_conflicts": 0,
                "removed_contextual_forms": removed_forms,
                "preserved_identity_forms": preserved_forms,
                "register": self.address_register,
                "register_source": self.address_register_source,
                "register_conflicts": sum(
                    len(item.get("removed_forms", [])) for item in self.register_cleanup
                ),
                "register_normalizations": sum(
                    len(item.get("normalizations", [])) for item in self.register_cleanup
                ),
            },
            "entries": entries,
            "form_cleanup": self.form_cleanup,
            "register_cleanup": self.register_cleanup,
            "style_profile": self.style_profile,
            "policy": self.policy.model_dump(),
            "metrics": dict(self.metrics),
        }

    async def prepare_dictionary(self, inputs):
        """Parse candidates from RAW, use VP as evidence, then freeze once."""
        inherited, problems = load_legacy(inputs.get("dictionary"))
        self.record_form_cleanup(problems)
        self.record_register_cleanup(problems)
        self.store.write("legacy-audit.json", problems)
        fatal = [item for item in problems if item.get("classification") == "fatal"]
        if fatal:
            raise QualityError(
                "Fatal inherited dictionary error: " + fatal[0].get("reason", "invalid mapping")
            )

        saved_index = self.store.read("terminology-index.json")
        if saved_index and saved_index.get("input_hash") == self.input_hash:
            self.index = saved_index["index"]
        else:
            self.progress("Local terminology scan")
            self.index = await asyncio.to_thread(
                build_index, self.pairs, self.alignments, inherited
            )
            self.store.write(
                "terminology-index.json", {"input_hash": self.input_hash, "index": self.index}
            )
        self.metrics.update(self.index.get("metrics", {}))
        self.local(
            "terminology_scan",
            f"RAW candidates={len(self.index.get('candidates', {}))}; ignored locally={len(self.index.get('report_only', {}))}",
        )

        saved = self.store.read("resolved-terms.json")
        if saved and saved.get("input_hash") in {self.input_hash, self.store.id}:
            loaded_terms = []
            for item in saved.get("terms", []):
                term = Term.model_validate(item)
                removed, preserved = clean_identity_forms(
                    term,
                    self.raw_contexts(),
                    require_evidence=True,
                )
                if removed:
                    self.record_form_cleanup(
                        [
                            {
                                "classification": "form_cleanup",
                                "source": term.source,
                                "removed_forms": removed,
                                "preserved_forms": preserved,
                                "reason": "removed checkpointed contextual character pseudo-forms",
                            }
                        ]
                    )
                self.apply_register_policy(
                    term, "aligned checkpointed address/title forms with the batch register"
                )
                if term.enforceable:
                    loaded_terms.append(term)
            self.terms = {term.source: term for term in loaded_terms}
            self.decisions = {
                source: state
                for source, state in saved.get("decisions", {}).items()
                if state in {"CONFIRMED", "IGNORE", "REUSED"}
            }
            self.outcomes = saved.get("outcomes", {})
        else:
            self.terms = {term.source: term for term in inherited if term.enforceable}
            self.decisions = {}

        self.ignored = {
            source: {**record, "state": "IGNORE"}
            for source, record in self.index.get("report_only", {}).items()
        }
        for source, record in self.ignored.items():
            self.outcomes[source] = {
                "source": source,
                "state": "IGNORE",
                "classification": record.get("classification", "ignored"),
                "reason": record.get("reason", "locally rejected candidate"),
            }

        conflicts = self.dictionary_conflicts()
        if conflicts:
            raise QualityError(
                "Confirmed dictionary has duplicate source identities: "
                + ", ".join(sorted(conflicts)[:20])
            )

        # Derive the batch style profile before the resolver runs, then apply it
        # to the confirmed set so the resolver sees one consistent register.
        self.refresh_style()
        for term in self.terms.values():
            self.apply_register_policy(
                term, "aligned confirmed address/title forms with the batch register"
            )

        pending = []
        for source, candidate in self.index.get("candidates", {}).items():
            if source in self.decisions:
                continue
            if self.migrate_legacy_resolution(source):
                continue
            if self.known_source(source):
                self.decisions[source] = "CONFIRMED"
                continue
            local_term = local_resolution(candidate, self.policy)
            if local_term:
                self.terms[source] = local_term
                self.decisions[source] = "CONFIRMED"
                self.outcomes[source] = {
                    "source": source,
                    "state": "CONFIRMED",
                    "translation": local_term.translation,
                    "reason": "local RAW/VietPhrase consensus",
                }
            elif candidate.get("frequency", 0):
                pending.append(source)
            else:
                self.decisions[source] = "IGNORE"

        self.save_decisions()
        self.progress(
            "Terminology candidates ready",
            raw_candidates=len(self.index.get("candidates", {})),
            resolver_candidates=len(pending),
            confirmed_terms=len(self.terms),
            ignored_candidates=len(self.ignored),
        )
        await self.resolve_candidates(pending)
        sanity(self.terms, allow_shared_translations=True)
        dictionary = export_dictionary(self.terms)
        self.store.write("working-dictionary.json", dictionary)
        self.store.write(
            "dictionary-audit.json",
            {
                "confirmed_terms": len(self.terms),
                "ignored_candidates": len(self.ignored),
                "ignored": sorted(
                    self.ignored.values(), key=lambda item: item.get("source", "")
                ),
            },
        )
        self.freeze(dictionary)
        self.store.write(
            "frozen-dictionary.json",
            {
                "input_hash": self.input_hash,
                "dictionary_hash": self.frozen_hash,
                "dictionary": dictionary,
            },
        )
        self.store.write("dictionary-resolution-report.json", self.dictionary_report())

    def freeze(self, dictionary):
        terms, invalid = load_legacy(dictionary)
        self.record_form_cleanup(invalid)
        self.record_register_cleanup(invalid)
        invalid = [
            problem
            for problem in invalid
            if problem.get("classification") not in AUDIT_ONLY_CLASSIFICATIONS
        ]
        if invalid:
            raise QualityError("Malformed frozen dictionary checkpoint")
        confirmed = {
            term.source: term.model_copy(deep=True) for term in terms if term.enforceable
        }
        sanity(confirmed, allow_shared_translations=True)
        self.terms = MappingProxyType(confirmed)
        self.refresh_style()
        self.frozen_hash = digest(export_dictionary(self.terms))
        self.progress(
            "Dictionary frozen",
            dictionary_hash=self.frozen_hash,
            dictionary_frozen=True,
            dictionary_summary=self.dictionary_report()["summary"],
        )
        self.local("dictionary_freeze", f"Frozen dictionary {self.frozen_hash}")

    def assert_frozen(self):
        if self.frozen_hash is None or digest(export_dictionary(self.terms)) != self.frozen_hash:
            raise QualityError("Frozen dictionary changed; stop and rebuild the batch")

    def check_unresolved(self, task, payload, translation):
        found = [
            candidate.model_dump()
            for candidate in translation.unresolved_terms
            if candidate.source in candidate.evidence
            and candidate.evidence
            in payload.get("raw_title", "") + "\n" + "\n".join(
                paragraph["text"] for paragraph in payload["raw"]
            )
        ]
        if found:
            self.store.write(
                f"ignored-terms/{digest(task)}.json",
                {"dictionary_hash": self.frozen_hash, "state": "IGNORE", "candidates": found},
            )

    @staticmethod
    def finding_data(issues):
        return [issue.model_dump(exclude_none=True) for issue in issues]

    def persist_validation_failure(self, task, issues, attempts):
        findings = self.finding_data(issues)
        self.store.write(
            f"validation-failures/{digest(task)}.json",
            {"task": task, "repair_attempts": attempts, "findings": findings},
        )
        self.store.log(
            {
                "status": "failed",
                "operation": "validation",
                "task": task,
                "message": f"Local validation still fails after {attempts} targeted repair(s)",
                "findings": findings,
            }
        )

    @staticmethod
    def validation_failure_text(task, issues, attempts):
        lines = [
            f"{task}: local validation still fails after {attempts} targeted repair(s)"
        ]
        for index, issue in enumerate(issues, 1):
            data = issue.model_dump(exclude_none=True)
            lines.append(
                f"{index}. source: {data.get('source', 'n/a')}; "
                f"required: {data.get('required_translation', 'n/a')}; "
                f"actual: {data.get('actual_text', 'n/a')}; "
                f"reason: {data.get('reason', data.get('kind', 'unknown'))}"
            )
        return "\n".join(lines)

    async def validate_and_repair(self, task, payload, translation, allow_repair=True):
        """Run deterministic checks and permit at most one repair for one chunk."""
        self.check_unresolved(task, payload, translation)
        try:
            issues = local_findings(payload, translation)
        except ValueError as exc:
            self.fail(task, str(exc))
        # Never trust findings serialized by an older validator.  Every resume
        # path reaches this point with the current RAW, frozen dictionary and
        # translated text, so the findings used for repair are freshly derived.
        self.store.write(
            f"validation-rejections/{digest(task)}.json",
            {
                "task": task,
                "pipeline_version": PIPELINE_VERSION,
                "dictionary_hash": self.frozen_hash,
                "findings": self.finding_data(issues),
                "recomputed": True,
            },
        )
        if not issues:
            self.local("normal_output_validation", f"{task}: local checks passed")
            return translation
        if not allow_repair:
            self.persist_validation_failure(task, issues, 0)
            self.fail(task, self.validation_failure_text(task, issues, 0))

        self.store.write(
            f"validation-rejections/{digest(task)}.json",
            {
                "task": task,
                "pipeline_version": PIPELINE_VERSION,
                "dictionary_hash": self.frozen_hash,
                "issues": self.finding_data(issues),
                "translation": translation.model_dump(),
            },
        )
        affected = sorted({issue.segment_id for issue in issues})
        selected = set(affected)
        chapter_key = task.split(":")[1]
        ids = [paragraph["id"] for paragraph in payload["raw"]]
        vp_ids = {
            value
            for group in self.alignments[chapter_key].groups
            if selected.intersection(group.raw)
            for value in group.vp
        }
        raw_text = "\n".join(
            [payload.get("raw_title", "") if -1 in selected else ""]
            + [paragraph["text"] for paragraph in payload["raw"] if paragraph["id"] in selected]
        )
        repair_payload = {
            "raw_title": payload.get("raw_title", ""),
            "vp_title": payload.get("vp_title", ""),
            "location": payload.get("location"),
            "raw": [paragraph for paragraph in payload["raw"] if paragraph["id"] in selected],
            "vp": [paragraph for paragraph in payload["vp"] if paragraph["id"] in vp_ids],
            "terminology": relevant(self.terms, raw_text),
            "dictionary_hash": self.frozen_hash,
            "affected_ids": affected,
            "issues": self.finding_data(issues),
            "validator_findings": self.finding_data(issues),
            "translation": {
                "title": translation.title,
                "segments": [
                    segment.model_dump()
                    for segment in translation.segments
                    if segment.id in selected
                ],
            },
            "repair_attempt": 1,
            "repair_feedback": "Fix only these findings; the confirmed dictionary is frozen and immutable.",
        }
        repaired = await self.ai(
            f"{task}:repair",
            prompts.REPAIR,
            repair_payload,
            Repair,
            operation="repair",
            reason="Concrete failed local validator findings",
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
        try:
            remaining = local_findings(payload, translation)
        except ValueError as exc:
            self.fail(task, str(exc))
        self.store.write(
            f"repair-validation/{digest(task)}.json",
            {
                "task": task,
                "repair_attempts": 1,
                "before": self.finding_data(issues),
                "after": self.finding_data(remaining),
            },
        )
        if remaining:
            self.persist_validation_failure(task, remaining, 1)
            self.fail(task, self.validation_failure_text(task, remaining, 1))
        self.local("repair_validation", f"{task}: one targeted repair passed")
        return translation

    async def chapter(self, pair):
        self.assert_frozen()
        raw, vp = pair
        key = raw.key
        whole = {
            "raw_title": raw.title,
            "vp_title": vp.title,
            "location": {"chapter": raw.number, "chunk": None},
            "raw": [{"id": index, "text": text} for index, text in enumerate(raw.paragraphs)],
            "vp": [{"id": index, "text": text} for index, text in enumerate(vp.paragraphs)],
            "terminology": relevant(self.terms, raw.title + "\n" + "\n".join(raw.paragraphs)),
        }
        saved = self.store.read(f"chapters/{key}.json")
        saved_dictionary_hash = saved.get("dictionary_hash") if saved else None
        saved_is_compatible = (
            saved
            and saved.get("pipeline_input_hash")
            in {self.input_hash, *self.compatible_input_hashes}
            and saved_dictionary_hash in {self.frozen_hash, *self.compatible_frozen_hashes}
        )
        if saved_is_compatible:
            try:
                checked = await self.validate_and_repair(
                    f"chapter:{key}:full",
                    whole,
                    Translation.model_validate(saved["translation"]),
                    allow_repair=False,
                )
            except QualityError:
                if saved_dictionary_hash not in self.compatible_frozen_hashes:
                    raise
                self.store.log(
                    {
                        "status": "local_success",
                        "operation": "dictionary_migration",
                        "message": f"Chapter {key} requires chunk-level revalidation after dictionary cleanup",
                    }
                )
            else:
                self.store.write(
                    f"chapters/{key}.json",
                    {
                        **saved,
                        "dictionary_hash": self.frozen_hash,
                        "pipeline_input_hash": self.input_hash,
                        "translation": checked.model_dump(),
                    },
                )
                return

        segments, title = [], ""
        for index, chunk in enumerate(self.chunks[key]):
            self.assert_frozen()
            self.activity[key] = {"chunk": index + 1, "chunks": len(self.chunks[key])}
            self.progress("Translating", active_chapters=dict(self.activity))
            chunk_terms = relevant(
                self.terms,
                raw.title + "\n" + "\n".join(item["text"] for item in chunk["raw"]),
            )
            payload = {
                "CHAPTER CONTEXT": {
                    **chapter_identity(raw),
                    "raw_title": raw.title,
                    "vp_title": vp.title,
                },
                "PREVIOUS CONTEXT": {
                    "instruction": "CONTEXT ONLY. Do not translate again.",
                    "paragraphs": [segment.text[-800:] for segment in segments[-2:]],
                },
                "RAW TO TRANSLATE": chunk["raw"],
                "VIETPHRASE REFERENCE": chunk["vp"],
                "TERMINOLOGY": chunk_terms,
                "dictionary_hash": self.frozen_hash,
            }
            chunk_hash = digest({"payload": payload, "pipeline_input_hash": self.input_hash})
            checkpoint = self.store.read(f"chunks/{key}-{index}.json")
            checkpoint_reusable = checkpoint and (
                checkpoint.get("input_hash") == chunk_hash
                or checkpoint.get("dictionary_hash") in self.compatible_frozen_hashes
            )
            if checkpoint_reusable:
                translated = Translation.model_validate(checkpoint["translation"])
            else:
                translated = await self.ai(
                    f"chapter:{key}:chunk:{index}:translate",
                    prompts.TRANSLATE,
                    payload,
                    Translation,
                    operation="translation",
                    reason="Translate RAW with frozen confirmed terminology",
                )
            translated = await self.validate_and_repair(
                f"chapter:{key}:chunk:{index}",
                {
                    **chunk,
                    "raw_title": raw.title,
                    "vp_title": vp.title,
                    "location": {"chapter": raw.number, "chunk": index},
                    "terminology": chunk_terms,
                },
                translated,
            )
            self.store.write(
                f"chunks/{key}-{index}.json",
                {
                    "input_hash": chunk_hash,
                    "dictionary_hash": self.frozen_hash,
                    "translation": translated.model_dump(),
                },
            )
            segments.extend(translated.segments)
            title = title or translated.title

        merged = await self.validate_and_repair(
            f"chapter:{key}:full",
            whole,
            Translation(title=title, segments=segments),
            allow_repair=False,
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
                self.store.read(f"chapters/{chapter.key}.json") is not None
                for chapter, _ in self.pairs
            ),
        )

    async def run(self):
        inputs = self.store.read("inputs.json")
        self.input_hash = pipeline_identity(inputs, self.policy.model_dump())
        self.progress(
            "Local parsing",
            error=None,
            error_detail=None,
            active_chapters={},
            dictionary_frozen=False,
        )
        parsed = self.store.read("parsed-chapters.json")
        if parsed and parsed.get("input_hash") == self.input_hash:
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
                    "pairs": [[raw.model_dump(), vp.model_dump()] for raw, vp in self.pairs],
                },
            )
        self.store.write("parser-version.json", {"version": PARSER_VERSION})
        await self.parallel(self.pairs, self.align)
        self.local("alignment", "RAW/VietPhrase structural alignment passed")
        self.local("chunk_construction", "Aligned chunks constructed locally")

        frozen = self.store.read("frozen-dictionary.json")
        if frozen:
            try:
                self.freeze(frozen["dictionary"])
            except (KeyError, TypeError, ValueError, QualityError):
                if frozen.get("input_hash") == self.input_hash:
                    raise
                frozen = None
            else:
                if frozen.get("input_hash") != self.input_hash:
                    self.compatible_input_hashes.add(frozen.get("input_hash"))
                if frozen.get("dictionary_hash") != self.frozen_hash:
                    self.compatible_frozen_hashes.add(frozen.get("dictionary_hash"))
                if (
                    frozen.get("input_hash") != self.input_hash
                    or frozen.get("dictionary_hash") != self.frozen_hash
                ):
                    self.store.write(
                        "frozen-dictionary.json",
                        {
                            "input_hash": self.input_hash,
                            "dictionary_hash": self.frozen_hash,
                            "dictionary": export_dictionary(self.terms),
                        },
                    )
        if not frozen:
            await self.prepare_dictionary(inputs)
        self.assert_frozen()
        self.refresh_style()
        self.store.write("dictionary-resolution-report.json", self.dictionary_report())
        await self.parallel(self.pairs, self.chapter)
        self.assert_frozen()

        self.progress("Local final global audit")
        chapters, seen = [], set()
        for raw, _ in self.pairs:
            saved = self.store.read(f"chapters/{raw.key}.json")
            if not saved:
                raise QualityError(f"Missing completed chapter {raw.key}")
            if raw.key in seen:
                raise QualityError(f"Duplicate chapter {raw.key} in final assembly")
            seen.add(raw.key)
            if saved["dictionary_hash"] != self.frozen_hash:
                raise QualityError(f"Chapter {raw.key} does not use the frozen dictionary")
            translation = saved["translation"]
            if not translation["title"].strip():
                raise QualityError(f"Empty translated title for chapter {raw.key}")
            chapters.append(
                {
                    **chapter_identity(raw),
                    "title": translation["title"],
                    "text": "\n\n".join(segment["text"] for segment in translation["segments"]),
                }
            )
        self.local("final_global_audit", f"{len(chapters)} chapters passed final integrity audit")
        self.store.write("translated.json", {"chapters": chapters})
        self.store.write(
            "translated.txt",
            "\n\n".join(
                f"{chapter['title']}\n{chapter['text']}" for chapter in chapters
            )
            + ("\n" if chapters else ""),
        )
        self.store.write(
            "dictionary.json",
            {
                **export_dictionary(self.terms),
                "dictionary_hash": self.frozen_hash,
                "resolution_report": self.dictionary_report(),
            },
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
