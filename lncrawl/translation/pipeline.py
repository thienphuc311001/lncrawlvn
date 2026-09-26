"""The single RAW-only, resumable Chinese-to-Vietnamese translation pipeline."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import re
from collections import defaultdict

from . import prompts
from .dictionary import (
    DictionaryConflict,
    best_evidence,
    candidate_filter_reason,
    TITLE_END,
    load_dictionary,
    load_rejected,
    load_unresolved,
    merge_patch,
    possible_type,
    relevant_entries,
    scan,
    validate_dictionary,
)
from .models import (
    CoverageAudit,
    DictionaryPatch,
    FALLBACK_MODEL,
    PARSER_VERSION,
    PIPELINE_VERSION,
    PRIMARY_MODEL,
    ProposedPatch,
    Rejected,
    Repair,
    Translation,
    TranslatedSegment,
)
from .manual_review import accepted_review, accepted_review_ids
from .notes import author_note_policy, is_author_note
from .parsing import ChapterValidationError, parse_chapters
from .scheduler import ErrorCategory, ProviderError
from .store import digest
from .validation import (
    SEMANTIC_SUSPICIONS,
    TITLE_WRAPPER,
    _segment_findings,
    deterministic_defects,
    local_findings,
    repair_integrity_findings,
    semantic_suspicions,
    source_quality_findings,
    structural_findings,
)


class QualityError(RuntimeError):
    pass


def _unresolved_merge(previous, additions, chapters):
    merged = {item["source"]: dict(item) for item in previous}
    for item in additions:
        source = item["source"]
        old = merged.get(source, {})
        evidence = []
        for value in (old.get("evidence") or []) + item.get("evidence", []):
            if isinstance(value, str):
                value = {"chapter": None, "text": value}
            if not isinstance(value, dict) or not value.get("text"):
                continue
            if not any(record.get("text") == value["text"]
                       and record.get("chapter") == value.get("chapter") for record in evidence):
                evidence.append({"chapter": value.get("chapter"), "text": value["text"]})
        by_chapter = dict(old.get("occurrences_by_chapter") or {})
        for chapter, count in (item.get("occurrences_by_chapter") or {}).items():
            by_chapter[str(chapter)] = max(int(by_chapter.get(str(chapter), 0)), int(count))
        old_by_chapter = old.get("occurrences_by_chapter") or {}
        item_by_chapter = item.get("occurrences_by_chapter") or {}
        unattributed = max(0, int(old.get("occurrences", 0))
                           - sum(int(value) for value in old_by_chapter.values()))
        unattributed += max(0, int(item.get("occurrences", item.get("frequency", 0)))
                            - sum(int(value) for value in item_by_chapter.values()))
        occurrence_count = sum(by_chapter.values()) + unattributed
        chapter_numbers = sorted({
            int(chapter) for chapter in (list(by_chapter) + list(old.get("chapters", []))
                                          + list(item.get("chapters", [])))
            if str(chapter).isdigit()
        })
        if not chapter_numbers:
            chapter_numbers = sorted(chapter.number for chapter in chapters
                                     if source in chapter.raw)
        evidence = best_evidence(evidence)
        incoming_kind = item.get("possible_type", "unknown")
        kind = (incoming_kind if incoming_kind != "unknown"
                else old.get("possible_type", incoming_kind))
        inferred = possible_type(source, evidence)
        if inferred and inferred != "unknown":
            kind = inferred
        first_seen = min(
            [value for value in (old.get("first_seen_chapter"), *chapter_numbers)
             if isinstance(value, int)], default=None,
        )
        merged[source] = {
            "source": source,
            "possible_type": kind,
            "reason": item.get("reason", old.get("reason", "insufficient evidence")),
            "evidence": evidence,
            "first_seen_chapter": first_seen,
            "last_seen_chapter": max(
                [value for value in (old.get("last_seen_chapter"), *(chapter_numbers))
                 if isinstance(value, int)], default=None,
            ),
            "chapters": chapter_numbers,
            "occurrences": occurrence_count,
            "occurrences_by_chapter": by_chapter,
            "resolve_attempts": max(int(old.get("resolve_attempts", 0)),
                                     int(item.get("resolve_attempts", 0))),
            "last_attempt_input_hash": item.get(
                "last_attempt_input_hash", old.get("last_attempt_input_hash")),
        }
        last_error = item.get("last_error", old.get("last_error"))
        if last_error:
            merged[source]["last_error"] = last_error
        else:
            merged[source].pop("last_error", None)
    return sorted(merged.values(), key=lambda item: item["source"])


def _candidate_batches(candidates, max_chars=32_000):
    """Batch at book scope; split only when evidence would exceed a useful payload."""
    batch = []
    size = 0
    for item in candidates:
        item_size = len(json.dumps(item, ensure_ascii=False))
        if batch and size + item_size > max_chars:
            yield batch
            batch, size = [], 0
        batch.append(item)
        size += item_size
    if batch:
        yield batch


def _observe_source(chapters, source):
    """Build bounded, chapter-labelled sentence evidence for one old candidate."""
    by_chapter = {}
    evidence = []
    for chapter in chapters:
        count = 0
        for paragraph in chapter.paragraphs:
            count += paragraph.count(source)
            position = paragraph.find(source)
            if position < 0:
                continue
            start = max(paragraph.rfind(mark, 0, position) for mark in "。！？!?；\n") + 1
            ends = [paragraph.find(mark, position + len(source)) for mark in "。！？!?；\n"]
            ends = [end for end in ends if end >= 0]
            end = min(ends) + 1 if ends else len(paragraph)
            sentence = paragraph[start:end].strip()
            if sentence and not any(item["text"] == sentence and item["chapter"] == chapter.number
                                     for item in evidence):
                evidence.append({"chapter": chapter.number, "text": sentence})
        if count:
            by_chapter[str(chapter.number)] = count
    return {
        "source": source,
        "possible_type": possible_type(source, evidence) or "unknown",
        "evidence": best_evidence(evidence),
        "chapters": [int(value) for value in by_chapter],
        "occurrences": sum(by_chapter.values()),
        "occurrences_by_chapter": by_chapter,
    }


def _split_paragraph(paragraph, limit):
    """Preserve one stable paragraph identity while splitting extreme prose safely."""
    if len(paragraph) <= limit:
        return [paragraph]
    sentences = re.findall(r".*?(?:[。！？!?][”’」』]?|$)", paragraph)
    sentences = [item for item in sentences if item]
    if "".join(sentences) != paragraph:
        raise QualityError("Source sentence splitting would change RAW text")
    if any(len(item) > limit for item in sentences):
        finer = []
        for sentence in sentences:
            if len(sentence) <= limit:
                finer.append(sentence)
            else:
                clauses = re.findall(r".*?(?:[，；][”’」』]?|$)", sentence)
                if "".join(clauses) != sentence or any(len(item) > limit for item in clauses if item):
                    raise QualityError("One source clause exceeds the safe translation request limit")
                finer.extend(item for item in clauses if item)
        sentences = finer
    pieces = []
    current = ""
    for sentence in sentences:
        if current and len(current) + len(sentence) > limit:
            pieces.append(current)
            current = ""
        current += sentence
    if current:
        pieces.append(current)
    return pieces


def _units(chapter, limit):
    """(request ID, RAW, persistent paragraph ID) tuples."""
    units = []
    for identifier, paragraph in chapter.paragraph_items:
        pieces = _split_paragraph(paragraph, limit)
        if len(pieces) == 1:
            units.append((identifier, paragraph, identifier))
        else:
            units.extend((f"{identifier}.F{index:04d}", piece, identifier)
                         for index, piece in enumerate(pieces, 1))
    return units


def _estimated_output_tokens(text):
    """Conservative local sizing signal; it is not a semantic quality gate."""
    han = sum("\u3400" <= char <= "\u9fff" for char in text)
    dialogue_marks = sum(char in "“”「」『』\"" for char in text)
    return int(han * 1.9 + max(0, len(text) - han) * 0.6 + dialogue_marks * 5)


def _groups(units, limit, max_paragraphs=40, output_token_limit=12_000):
    if (sum(len(unit[1]) for unit in units) <= limit
            and sum(_estimated_output_tokens(unit[1]) for unit in units) <= output_token_limit):
        return [units] if units else []
    groups = []
    group = []
    size = 0
    estimated = 0
    for unit in units:
        unit_tokens = _estimated_output_tokens(unit[1])
        if group and (size + len(unit[1]) > limit
                      or estimated + unit_tokens > output_token_limit
                      or len(group) >= max_paragraphs):
            groups.append(group)
            group, size, estimated = [], 0, 0
        group.append(unit)
        size += len(unit[1])
        estimated += unit_tokens
    if group:
        groups.append(group)
    return groups


def _group_hash(chapter, group, dictionary, previous, lookahead, is_first):
    return digest({
        "chapter": chapter.number,
        "title": chapter.title if is_first else "",
        "raw": [(identifier, text) for identifier, text, _ in group],
        "dictionary": dictionary,
        "previous_translation": previous,
        "next_source": lookahead,
        "policy": prompts.POLICY_VERSIONS["translation"],
        "schema": Translation.model_json_schema(),
        "thinking_budget": os.getenv("TRANSLATION_THINKING_BUDGET"),
    })


class Pipeline:
    def __init__(self, store, scheduler):
        self.store = store
        self.scheduler = scheduler
        self.chapters = []
        self.dictionary = None
        self.unresolved = []
        self.rejected = []
        self.pre_hash = None
        self.input_hash = None
        self.legacy_raw = {}
        self.dictionary_migrations = []
        self.ratio_baseline = []
        self.paragraph_ratio_baseline = []
        self.validation_baselines = {}

    def _remember_ratios(self, chapter, translation):
        self.ratio_baseline.append(
            sum(len(item.text) for item in translation.segments)
            / max(1, sum(len(raw) for raw in chapter.paragraphs))
        )
        self.paragraph_ratio_baseline.extend(
            len(item.text) / max(1, len(raw))
            for raw, item in zip(chapter.paragraphs, translation.segments)
        )

    def _local_findings(self, chapter, translation):
        chapter_ratios, paragraph_ratios = self.validation_baselines.get(
            chapter.number, (self.ratio_baseline, self.paragraph_ratio_baseline))
        findings = local_findings(
            chapter, translation, self.dictionary,
            ratio_baseline=chapter_ratios,
            paragraph_ratio_baseline=paragraph_ratios,
        )
        raw_by_id = dict(chapter.paragraph_items)
        text_by_id = {segment.id: segment.text for segment in translation.segments}
        accepted = {}
        for finding in findings:
            identifier = finding.get("id")
            if identifier not in raw_by_id or identifier not in text_by_id:
                continue
            if identifier not in accepted:
                accepted[identifier] = accepted_review(
                    self.store, chapter.number, identifier, raw_by_id[identifier],
                    text_by_id[identifier], self.dictionary)
        return [finding for finding in findings
                if not (finding.get("id") in accepted
                        and accepted[finding["id"]]
                        and finding in accepted[finding["id"]].get("accepted_findings", []))]

    def event(self, name, **fields):
        self.store.log({"event": name, "status": name.lower(), **fields})

    def progress(self, stage, **fields):
        complete = sum(self.store.read(f"chapters/{chapter.key}.json") is not None
                       for chapter in self.chapters)
        self.store.progress(
            "running", stage=stage, total_chapters=len(self.chapters),
            completed_chapters=complete, chapters_finalized=complete,
            request_statistics=self.store.request_statistics(), **fields,
        )

    def _operation(self, event):
        return {
            "PRE_DICTIONARY_REQUEST": "pre_dictionary",
            "POST_DICTIONARY_REQUEST": "post_dictionary",
            "TRANSLATION_REQUEST": "translation",
            "QA_REQUEST": "qa",
            "REPAIR_REQUEST": "repair",
        }[event]

    def _guard_requests(self):
        statistics = self.store.request_statistics()
        count = statistics["logical_call_count"]
        translation_calls = statistics["by_operation"].get("translation", {}).get("logical_calls", 0)
        repair_calls = statistics["by_operation"].get("repair", {}).get("logical_calls", 0)
        if (translation_calls and repair_calls > translation_calls
                and self.store.read("request-warning.json", {}).get("code") != "REPAIR_VOLUME_ANOMALY"):
            self.store.write("request-warning.json", {
                "code": "REPAIR_VOLUME_ANOMALY", "severity": "severe",
                "logical_calls": count, "breakdown": statistics["by_operation"],
            })
            self.event("REQUEST_BUDGET_ANOMALY", logical_calls=count,
                       breakdown=statistics["by_operation"], severity="severe",
                       reason="Repair calls exceed translation calls")
        warning = int(os.getenv("TRANSLATION_WARNING_LOGICAL_CALLS",
                                str(max(20, 10 * len(self.chapters)))))
        hard = int(os.getenv("TRANSLATION_HARD_LOGICAL_CALLS",
                             str(max(30, 15 * len(self.chapters)))))
        if count == warning:
            self.store.write("request-warning.json", {
                "code": "REQUEST_BUDGET_ANOMALY", "severity": "warning",
                "logical_calls": count,
                "breakdown": self.store.request_statistics()["by_operation"],
            })
            self.event("REQUEST_BUDGET_ANOMALY", logical_calls=count,
                       breakdown=self.store.request_statistics()["by_operation"], severity="warning")
        if count >= hard:
            self.store.write("request-warning.json", {
                "code": "REQUEST_BUDGET_ANOMALY", "severity": "fatal",
                "logical_calls": count,
                "breakdown": self.store.request_statistics()["by_operation"],
            })
            self.event("REQUEST_BUDGET_ANOMALY", logical_calls=count,
                       breakdown=self.store.request_statistics()["by_operation"], severity="fatal")
            raise QualityError(f"Request budget exceeded: {count} semantic logical calls")

    def _fingerprint(self, operation, policy, payload, schema, requested_model):
        return digest({
            "operation": operation,
            "policy_version": prompts.POLICY_VERSIONS[operation],
            "policy": policy,
            "payload": payload,
            "schema": schema.model_json_schema(),
            "requested_model": requested_model,
            "thinking_budget": os.getenv("TRANSLATION_THINKING_BUDGET"),
            "context_strategy": "previous-4-next-1-adaptive-v2",
        })

    async def request(self, event, policy, payload, schema, chapter=None,
                      requested_model=None, cache=False):
        operation = self._operation(event)
        requested_model = requested_model or (FALLBACK_MODEL if operation in ("qa", "repair")
                                             else PRIMARY_MODEL)
        fingerprint = self._fingerprint(operation, policy, payload, schema, requested_model)
        if cache:
            saved = self.store.cache_get(fingerprint)
            if saved:
                parsed = schema.model_validate(saved["value"])
                object.__setattr__(parsed, "_request_meta", saved.get("metadata", {}))
                self.store.account(operation, "cache_hit")
                self.store.chapter_account(chapter, operation, "cache_hit")
                self.event("CACHE_HIT", operation=operation, chapter=chapter,
                           fingerprint=fingerprint)
                return parsed
        self._guard_requests()
        self.store.account(operation, "logical")
        self.store.chapter_account(chapter, operation, "logical")
        raw_value = payload.get("raw", [])
        raw_chars = sum(len(item.get("text", "")) for item in raw_value) if isinstance(raw_value, list) else len(str(raw_value))
        relevant = payload.get("dictionary", payload.get("locked_dictionary", []))
        self.event(event, chapter=chapter, requested_model=requested_model,
                   raw_chars=raw_chars, dictionary_entry_count=len(relevant),
                   alias_count=sum(len(item.get("aliases", [])) for item in relevant),
                   raw_ids=[item.get("id") for item in raw_value] if isinstance(raw_value, list) else [])

        def record(metadata):
            status = metadata.get("status")
            if status == "running":
                attempt_count = self.store.request_statistics()["api_attempt_count"]
                attempt_hard = int(os.getenv("TRANSLATION_HARD_API_ATTEMPTS",
                                              str(max(60, 30 * len(self.chapters)))))
                if attempt_count >= attempt_hard:
                    self.store.write("request-warning.json", {
                        "code": "REQUEST_BUDGET_ANOMALY", "severity": "fatal",
                        "logical_calls": self.store.request_statistics()["logical_call_count"],
                        "api_attempts": attempt_count,
                        "breakdown": self.store.request_statistics()["by_operation"],
                    })
                    self.event("REQUEST_BUDGET_ANOMALY", api_attempts=attempt_count,
                               severity="fatal", reason="API attempt guard exceeded")
                    raise QualityError(f"API attempt budget exceeded: {attempt_count}")
                self.store.account(operation, "attempt")
                self.store.chapter_account(chapter, operation, "attempt", metadata.get("model"))
                if metadata.get("technical_retry"):
                    self.store.account(operation, "technical_retry")
                    self.store.chapter_account(chapter, operation, "technical_retry")
                if metadata.get("key_rotated"):
                    self.event("KEY_ROTATION", chapter=chapter, operation=operation,
                               actual_model=metadata.get("model"), key_slot=metadata.get("key_slot"))
                if metadata.get("model_fallback_started"):
                    self.store.account(operation, "fallback")
                    self.store.chapter_account(chapter, operation, "fallback")
                    self.event("MODEL_FALLBACK", chapter=chapter, operation=operation,
                               from_model=requested_model, to_model=metadata.get("model"))
            elif status == "retrying":
                self.event("TECHNICAL_RETRY", chapter=chapter, operation=operation,
                           actual_model=metadata.get("model"), attempt=metadata.get("attempt"))
            elif status == "daily_quota_disabled":
                self.event("DAILY_QUOTA_DISABLED", chapter=chapter, operation=operation,
                           actual_model=metadata.get("model"), key_slot=metadata.get("key_slot"))
            elif status == "rate_limit_cooldown":
                self.event("RATE_LIMIT_COOLDOWN", chapter=chapter, operation=operation,
                           actual_model=metadata.get("model"), key_slot=metadata.get("key_slot"),
                           retry_after=metadata.get("retry_after"))
            self.store.diagnostic({"event": event, "operation": operation, "chapter": chapter,
                                   "requested_model": requested_model,
                                   "actual_model": metadata.get("model"), **metadata})

        result = await self.scheduler.request(policy, payload, schema, record,
                                              requested_model=requested_model,
                                              allow_fallback=requested_model == PRIMARY_MODEL)
        if cache:
            self.store.cache_put(fingerprint, result.model_dump(),
                                 getattr(result, "_request_meta", {}))
        return result

    async def resolve(self, candidates, phase, start_offset=0):
        if not candidates:
            return
        raw = "\n".join(chapter.raw for chapter in self.chapters)
        offset = start_offset
        for batch in _candidate_batches(candidates):
            batch = [item for item in batch if not candidate_filter_reason(item)]
            if not batch:
                continue
            owned = validate_dictionary(self.dictionary)
            rejected_sources = {item["source"] for item in self.rejected}
            batch = [item for item in batch
                     if item["source"] not in owned and item["source"] not in rejected_sources]
            if not batch:
                continue
            known = {item["source"] for item in batch}
            evidence_text = "\n".join(
                "\n".join(value.get("text", "") if isinstance(value, dict) else value
                           for value in item.get("evidence", [])) for item in batch
            )
            relevant = relevant_entries(self.dictionary, evidence_text)
            relevant_sources = {entry["source"] for entry in relevant}
            for entry in self.dictionary["entries"]:
                if (entry["type"] == "character" and entry["source"] not in relevant_sources
                        and any(item["source"].startswith(entry["source"][:1]) for item in batch)):
                    relevant.append(entry)
                    relevant_sources.add(entry["source"])
            payload = {"candidates": batch, "locked_dictionary": relevant,
                       "rule": "Confirm only proven identity and reusable terminology; leave uncertainty unresolved."}
            event = "PRE_DICTIONARY_REQUEST" if phase == "pre" else "POST_DICTIONARY_REQUEST"
            provider_error = None
            try:
                proposal = await self.request(event, prompts.DICTIONARY, payload,
                                              ProposedPatch, cache=True)
            except ProviderError as exc:
                proposal = None
                provider_error = str(exc)
                self.event("DICTIONARY_RESOLUTION_TECHNICAL_FAILURE", phase=phase,
                           sources=sorted(known), reason=provider_error[:500])
            self.event("DICTIONARY_PATCH_RECEIVED", phase=phase,
                       operations=len(proposal.confirmed) if proposal else 0)
            patch = DictionaryPatch.model_validate(proposal.model_dump()) if proposal else DictionaryPatch()
            explicit_unresolved = {item.source: item for item in patch.unresolved}
            explicit_rejected = {item.source: item for item in patch.rejected}
            confirmed_proposal_sources = {
                operation.entry.source if operation.operation == "add_entry" else operation.alias.source
                for operation in patch.confirmed
            }
            invalid_sources = {
                source for source in known
                if sum(source in outcome for outcome in (
                    confirmed_proposal_sources, set(explicit_unresolved), set(explicit_rejected),
                )) > 1
            }
            for source in invalid_sources:
                self.event("DICTIONARY_PATCH_REJECTED", phase=phase, source=source,
                           reason="Candidate has conflicting resolver outcomes")
            merged = self.dictionary
            confirmed = set()
            operations = sorted(patch.confirmed,
                                key=lambda item: 0 if item.operation == "add_entry" else 1)
            for operation in operations:
                source = operation.entry.source if operation.operation == "add_entry" else operation.alias.source
                if source not in known:
                    self.event("DICTIONARY_PATCH_REJECTED", phase=phase, source=source,
                               reason="Unsolicited source")
                    continue
                if source in invalid_sources:
                    continue
                try:
                    if operation.operation == "add_entry" and TITLE_END.search(source):
                        raise DictionaryConflict(
                            f"Title-form source requires an existing canonical owner: {source}"
                        )
                    merged = merge_patch(merged, {"confirmed": [operation.model_dump()],
                                                  "unresolved": []}, raw)
                except DictionaryConflict as exc:
                    reason = str(exc)
                    self.event("DICTIONARY_PATCH_REJECTED", phase=phase, source=source, reason=reason)
                    # Ownership conflicts are terminal for this candidate. Other
                    # local validation failures are technical and remain retryable.
                    if any(token in reason.lower() for token in ("locked", "conflict", "owned")):
                        patch.rejected.append(Rejected(source=source, reason=reason[:300]))
                    else:
                        invalid_sources.add(source)
                    continue
                confirmed.add(source)
                if operation.operation == "add_entry":
                    confirmed.update(alias.source for alias in operation.entry.aliases)
            candidate_by_source = {item["source"]: item for item in batch}
            outcomes = [set(confirmed), set(explicit_unresolved), set(explicit_rejected)]
            for source in known:
                if sum(source in outcome for outcome in outcomes) > 1:
                    invalid_sources.add(source)
            explicitly_rejected = []
            for item in patch.rejected:
                if item.source in known and item.source not in confirmed and item.source not in invalid_sources:
                    explicitly_rejected.append({"source": item.source, "reason": item.reason})
            for source in known:
                state = candidate_by_source[source]
                state["resolve_attempts"] = int(state.get("resolve_attempts", 0)) + 1
                state["last_attempt_input_hash"] = self.input_hash
                if source in confirmed:
                    continue
                if source in explicit_rejected and source not in invalid_sources:
                    continue
                if source in invalid_sources or provider_error:
                    state["last_error"] = provider_error or "Invalid provider patch schema"
                else:
                    state.pop("last_error", None)
                    unresolved_record = explicit_unresolved.get(source)
                    state["reason"] = (unresolved_record.reason if unresolved_record
                                        else "Resolver left candidate unresolved")
                    if unresolved_record and unresolved_record.possible_type != "unknown":
                        state["possible_type"] = unresolved_record.possible_type
                self.unresolved = _unresolved_merge(
                    [item for item in self.unresolved if item["source"] != source],
                    [state], self.chapters,
                )
            self.dictionary = merged
            removed = confirmed | {item["source"] for item in explicitly_rejected}
            self.unresolved = [item for item in self.unresolved if item["source"] not in removed]
            rejected = {item["source"]: item for item in self.rejected}
            for item in explicitly_rejected:
                rejected[item["source"]] = item
            for source in confirmed:
                rejected.pop(source, None)
            self.rejected = [rejected[source] for source in sorted(rejected)]
            offset += len(batch)
            self.store.write(f"{phase}-dictionary-progress.json", {
                "input_hash": self.input_hash, "offset": offset,
                "pre_hash": self.pre_hash if phase == "post" else None,
                "dictionary": self.dictionary, "unresolved": self.unresolved,
                "rejected": self.rejected,
            })
            self.event("DICTIONARY_PATCH_VALIDATED", phase=phase, confirmed=len(confirmed))
            self.event("DICTIONARY_MERGED", phase=phase, entries=len(self.dictionary["entries"]))

    async def pre_dictionary(self, inputs):
        saved = self.store.read("pre-dictionary.json")
        if saved and saved.get("input_hash") == self.input_hash:
            self.dictionary = load_dictionary(saved["dictionary"], self.dictionary_migrations)
            self.event("DICTIONARY_LOADED", entries=len(self.dictionary["entries"]), reused=True)
            self.unresolved = load_unresolved({"unresolved": saved.get("unresolved", [])})
            self.rejected = load_rejected({"rejected": self.store.read("rejected.json", []) or []})
            if self.dictionary_migrations:
                self.store.write("dictionary-migration.json", {
                    "target_version": self.dictionary["version"],
                    "operations": self.dictionary_migrations,
                })
                self.store.write("pre-dictionary.json", {
                    "input_hash": self.input_hash, "dictionary": self.dictionary,
                    "unresolved": self.unresolved,
                })
            self.pre_hash = digest(self.dictionary)
            return
        self.dictionary = load_dictionary(inputs.get("dictionary"), self.dictionary_migrations)
        self.rejected = load_rejected(inputs.get("dictionary"))
        local_rejected = load_rejected({"rejected": self.store.read("rejected.json", []) or []})
        self.rejected = load_rejected({"rejected": self.rejected + local_rejected})
        self.event("DICTIONARY_LOADED", entries=len(self.dictionary["entries"]), reused=False)
        frozen = self.store.read("frozen-dictionary.json")
        if frozen and isinstance(frozen.get("dictionary"), dict):
            legacy = load_dictionary(frozen["dictionary"], self.dictionary_migrations)
            existing = {entry["source"]: entry for entry in self.dictionary["entries"]}
            operations = []
            for entry in legacy["entries"]:
                current = existing.get(entry["source"])
                if current is None:
                    operations.append({"operation": "add_entry", "entry": entry})
                else:
                    if any(current[field] != entry[field]
                           for field in ("translation", "type", "gender", "status")):
                        raise DictionaryConflict(f"Legacy frozen mapping conflicts with input: {entry['source']}")
                    operations.extend({"operation": "add_alias", "canonical_source": entry["source"],
                                       "alias": alias} for alias in entry["aliases"])
            self.dictionary = merge_patch(self.dictionary, {"confirmed": operations, "unresolved": []},
                                          inputs["raw"], allow_unattested=True)
            self.event("DICTIONARY_MERGED", phase="legacy_migration",
                       entries=len(self.dictionary["entries"]))
        if self.dictionary_migrations:
            self.store.write("dictionary-migration.json", {
                "target_version": self.dictionary["version"],
                "operations": self.dictionary_migrations,
            })
        self.unresolved = _unresolved_merge(load_unresolved(inputs.get("dictionary")),
                                            load_unresolved({"unresolved":
                                                self.store.read("unresolved.json", []) or []}),
                                            self.chapters)
        locked_sources = validate_dictionary(self.dictionary)
        self.unresolved = [item for item in self.unresolved if item["source"] not in locked_sources]
        rejection_sources = {item["source"] for item in self.rejected}
        self.unresolved = [item for item in self.unresolved if item["source"] not in rejection_sources]
        self.event("PRE_SCAN_STARTED")
        scanned = scan(self.chapters, self.dictionary)
        scanned_sources = {item["source"] for item in scanned}
        for item in self.unresolved:
            if item["source"] not in scanned_sources:
                observed = _observe_source(self.chapters, item["source"])
                if observed["occurrences"]:
                    scanned.append(observed)
        old_by_source = {item["source"]: item for item in self.unresolved}
        resolver_candidates = []
        reused_unresolved = 0
        for item in scanned:
            source = item["source"]
            if source in locked_sources or source in rejection_sources or candidate_filter_reason(item):
                continue
            old = old_by_source.get(source)
            old_evidence = old.get("evidence", []) if old else []
            old_keys = {(value.get("chapter"), value.get("text")) for value in old_evidence
                        if isinstance(value, dict)}
            new_keys = {(value.get("chapter"), value.get("text")) for value in item.get("evidence", [])
                        if isinstance(value, dict)}
            changed_evidence = bool(new_keys - old_keys)
            merged = _unresolved_merge([old] if old else [], [item], self.chapters)[0]
            changed_occurrences = not old or merged["occurrences"] > old.get("occurrences", 0)
            improved_type = bool(old and old.get("possible_type") == "unknown"
                                 and merged["possible_type"] != "unknown")
            retry_failure = bool(old and old.get("last_error")
                                 and old.get("last_attempt_input_hash") != self.input_hash)
            self.unresolved = _unresolved_merge(
                [record for record in self.unresolved if record["source"] != source],
                [merged], self.chapters,
            )
            if changed_occurrences or changed_evidence or improved_type or retry_failure:
                resolver_candidates.append(merged)
            elif old:
                reused_unresolved += 1
        self.unresolved = sorted(self.unresolved, key=lambda item: item["source"])
        self.event("PRE_SCAN_COMPLETED", candidates=len(resolver_candidates),
                   reused_unresolved=reused_unresolved)
        self.progress("Pre-dictionary resolution", candidate_count=len(resolver_candidates))
        partial = self.store.read("pre-dictionary-progress.json")
        offset = 0
        if partial and partial.get("input_hash") == self.input_hash:
            self.dictionary = load_dictionary(partial["dictionary"])
            self.unresolved = load_unresolved({"unresolved": partial.get("unresolved", [])})
            self.rejected = load_rejected({"rejected": partial.get("rejected", [])})
            offset = partial["offset"]
        await self.resolve(resolver_candidates[offset:], "pre", offset)
        self.pre_hash = digest(self.dictionary)
        self.store.write("pre-dictionary.json", {
            "input_hash": self.input_hash, "dictionary": self.dictionary,
            "unresolved": self.unresolved,
        })
        self.store.write("unresolved.json", self.unresolved)
        self.store.write("rejected.json", self.rejected)
        self.event("CHECKPOINT_SAVED", stage="pre_dictionary")

    def _convert_legacy_translation(self, chapter, value):
        title = re.sub(rf"^Chương\s+{chapter.number}\s*[:：.-]?\s*", "", value["title"], flags=re.I)
        segments = value["segments"]
        if [part.get("id") for part in segments] == list(range(len(chapter.paragraphs))):
            segments = [{"id": identifier, "text": part["text"]}
                        for identifier, part in zip(chapter.paragraph_ids, segments)]
        return Translation.model_validate({"title": title, "segments": segments})

    def _valid_checkpoint(self, chapter):
        saved = self.store.read(f"chapters/{chapter.key}.json")
        if not saved:
            return None
        if saved.get("pipeline_version") not in (None, PIPELINE_VERSION):
            return None
        if saved.get("qa_status") not in (None, "pass", "manual_override"):
            return None
        if saved.get("raw_hash"):
            if saved["raw_hash"] != digest((chapter.title, chapter.paragraphs)):
                raise QualityError(f"Chapter {chapter.number} checkpoint RAW mismatch")
            if (saved.get("pre_dictionary_hash") != self.pre_hash
                    and saved.get("pipeline_version") == PIPELINE_VERSION
                    and not self.dictionary_migrations):
                raise QualityError(f"Chapter {chapter.number} checkpoint dictionary mismatch")
        else:
            old = self.legacy_raw.get(chapter.key)
            if (not old or old.get("title") != chapter.title
                    or tuple(old.get("paragraphs", [])) != chapter.paragraphs):
                return None
        try:
            translated = self._convert_legacy_translation(chapter, saved["translation"])
        except (KeyError, TypeError, ValueError):
            return None
        findings = self._local_findings(chapter, translated)
        if saved.get("pipeline_version") == PIPELINE_VERSION:
            audited = set(saved.get("audited_ids", []))
            findings = [finding for finding in findings
                        if not (finding["kind"] in SEMANTIC_SUSPICIONS
                                and finding.get("id") in audited)]
        if findings:
            return None
        if saved.get("pipeline_version") != PIPELINE_VERSION:
            saved.update({
                "raw_hash": digest((chapter.title, chapter.paragraphs)),
                "pre_dictionary_hash": self.pre_hash,
                "translation": translated.model_dump(),
                "qa_status": "pass", "state": "committed",
                "pipeline_version": PIPELINE_VERSION,
                "migrated": True,
            })
            self.store.write(f"chapters/{chapter.key}.json", saved)
            self.event("CHECKPOINT_SAVED", chapter=chapter.number, migrated=True)
        self._remember_ratios(chapter, translated)
        return translated

    async def _translate_group(self, chapter, group, group_number, previous, lookahead,
                               is_first=True):
        source_by_id = dict(chapter.paragraph_items)
        raw = [{"id": identifier, "text": text,
                "role": "author_note" if is_author_note(source_by_id[original]) else "narrative"}
               for identifier, text, original in group]
        relevant = relevant_entries(self.dictionary, "\n".join(text for _, text, _ in group))
        payload = {
            "chapter": chapter.number,
            "title": chapter.title if is_first else "",
            "raw": raw,
            "dictionary": relevant,
            "previous_translation": previous[-4:],
            "next_source": lookahead,
        }
        fingerprint = _group_hash(chapter, group, relevant, previous[-4:], lookahead, is_first)
        saved = self.store.read(f"accepted-groups/{chapter.key}/{fingerprint}.json")
        if saved and saved.get("state") == "validated" and saved.get("fingerprint") == fingerprint:
            response = Translation.model_validate(saved["translation"])
            if not structural_findings([item["id"] for item in raw], response.segments):
                self.store.account("translation", "cache_hit")
                self.store.chapter_account(chapter.number, "translation", "cache_hit")
                self.event("CACHE_HIT", operation="translation", chapter=chapter.number,
                           group=group_number)
                return response, saved.get("request_metadata", {}), True
        if os.getenv("TRANSLATION_DEBUG_SNAPSHOTS") == "1":
            self.store.write(f"debug/chapter-{chapter.key}-part-{group_number}.json", {
                "model": PRIMARY_MODEL, "system_policy_version": prompts.POLICY_VERSIONS["translation"],
                "system_instruction": prompts.TRANSLATE, "payload": payload,
                "generation_config": {"thinking_budget": os.getenv("TRANSLATION_THINKING_BUDGET")},
            })
        last_failure = None
        for requested_model in (PRIMARY_MODEL, FALLBACK_MODEL):
            if requested_model == FALLBACK_MODEL:
                self.store.account("translation", "fallback")
                self.store.chapter_account(chapter.number, "translation", "fallback")
                self.event("MODEL_FALLBACK", chapter=chapter.number, operation="translation",
                           from_model=PRIMARY_MODEL, to_model=FALLBACK_MODEL,
                           reason="invalid_structural_response")
            try:
                response = await self.request("TRANSLATION_REQUEST", prompts.TRANSLATE,
                                              payload, Translation, chapter.number,
                                              requested_model=requested_model)
            except ProviderError as exc:
                raise
            meta = getattr(response, "_request_meta", {})
            findings = structural_findings([item["id"] for item in raw], response.segments)
            if is_first and chapter.title and not response.title.strip():
                findings.append({"kind": "empty_title", "id": chapter.title_id})
            if not findings:
                self.event("TRANSLATION_RESPONSE", chapter=chapter.number, group=group_number,
                           requested_model=requested_model,
                           actual_model=meta.get("actual_model", requested_model),
                           finish_reason=meta.get("finish_reason", "STOP"),
                           output_chars=sum(len(item.text) for item in response.segments))
                unit_findings = [finding for identifier, raw_text, _ in group
                                 for segment in response.segments if segment.id == identifier
                                 for finding in _segment_findings(identifier, raw_text,
                                                                  segment.text, self.dictionary)]
                if is_first and chapter.title:
                    unit_findings.extend(_segment_findings(
                        chapter.title_id, chapter.title, response.title,
                        self.dictionary, check_truncation=False))
                    if TITLE_WRAPPER.match(response.title):
                        unit_findings.append({"kind": "malformed_title_wrapper",
                                              "id": chapter.title_id})
                locally_accepted = not unit_findings
                if locally_accepted:
                    self.store.write(f"accepted-groups/{chapter.key}/{fingerprint}.json", {
                        "fingerprint": fingerprint, "state": "validated",
                        "translation": response.model_dump(), "request_metadata": meta,
                    })
                    self.event("CHECKPOINT_SAVED", chapter=chapter.number, group=group_number,
                               stage="accepted_translation_group")
                return response, meta, locally_accepted
            self.event("TRANSLATION_VALIDATION_FAIL", chapter=chapter.number,
                       group=group_number, findings=findings)
            last_failure = QualityError(f"Chapter {chapter.number} group {group_number} invalid IDs/title: {findings}")
            if meta.get("actual_model") == FALLBACK_MODEL:
                break
        raise last_failure

    async def _translate_group_with_recovery(self, chapter, group, group_number, previous,
                                             lookahead, is_first=True):
        try:
            return [await self._translate_group(chapter, group, group_number,
                                                previous, lookahead, is_first)]
        except ProviderError as exc:
            if exc.category is not ErrorCategory.INCOMPLETE_GENERATION:
                raise
        except QualityError:
            pass
        if len(group) == 1:
            identifier, raw, original = group[0]
            pieces = _split_paragraph(raw, max(100, len(raw) // 2))
            if len(pieces) < 2:
                raise QualityError(f"Chapter {chapter.number} has an incomplete single-unit generation")
            subgroup = [(f"{identifier}.F{index:04d}", piece, original)
                        for index, piece in enumerate(pieces, 1)]
            return await self._translate_group_with_recovery(chapter, subgroup, group_number,
                                                              previous, lookahead, is_first)
        cut = len(group) // 2
        self.event("TRANSLATION_INCOMPLETE", chapter=chapter.number, group=group_number,
                   reason="Incomplete response or invalid structure; splitting affected group",
                   groups=[len(group[:cut]), len(group[cut:])])
        left = await self._translate_group_with_recovery(chapter, group[:cut], group_number,
                                                         previous, group[cut][1], is_first)
        left_texts = [segment.text for response, _, accepted in left if accepted
                      for segment in response.segments]
        right_previous = (previous + left_texts)[-4:]
        right = await self._translate_group_with_recovery(chapter, group[cut:], group_number + 1,
                                                          right_previous, lookahead, False)
        return left + right

    def _raw_by_id(self, chapter):
        return {**dict(chapter.paragraph_items), chapter.title_id: chapter.title}

    def _text_by_id(self, chapter, result, identifier):
        if identifier == chapter.title_id:
            return result.title
        return next(item.text for item in result.segments if item.id == identifier)

    def _set_text_by_id(self, chapter, result, identifier, text):
        if identifier == chapter.title_id:
            result.title = text
        else:
            next(item for item in result.segments if item.id == identifier).text = text

    async def _audit_suspicions(self, chapter, result, findings):
        suspects = list(dict.fromkeys(item["id"] for item in findings if item.get("id")))
        raw_by_id = self._raw_by_id(chapter)
        if not suspects:
            raise QualityError(f"Chapter {chapter.number} has unlocalized QA suspicion")
        positions = {identifier: index for index, identifier in enumerate(chapter.paragraph_ids)}
        context = []
        for identifier in suspects:
            index = positions.get(identifier)
            context.append({
                "id": identifier,
                "previous_raw": chapter.paragraphs[index - 1] if index is not None and index > 0 else "",
                "previous_translation": result.segments[index - 1].text
                if index is not None and index > 0 else "",
                "next_raw": chapter.paragraphs[index + 1]
                if index is not None and index + 1 < len(chapter.paragraphs) else "",
            })
        payload = {
            "raw": [{"id": identifier, "text": raw_by_id[identifier]} for identifier in suspects],
            "vietnamese": [{"id": identifier, "text": self._text_by_id(chapter, result, identifier)}
                           for identifier in suspects],
            "dictionary": relevant_entries(self.dictionary,
                                           "\n".join(raw_by_id[identifier] for identifier in suspects)),
            "local_findings": findings,
            "context": context,
        }
        self.event("QA_REQUEST", chapter=chapter.number, suspects=suspects)
        audit = await self.request("QA_REQUEST", prompts.QA, payload, CoverageAudit,
                                   chapter.number, cache=True)
        confirmed = []
        for finding in audit.findings:
            if finding.id not in suspects:
                raise QualityError(f"Chapter {chapter.number} QA named an unrequested ID")
            confirmed.append({"kind": finding.kind, "id": finding.id,
                              "reason": finding.reason, "confirmed_by": "semantic_qa"})
        self.event("QA_DEFECT_CONFIRMED" if confirmed else "QA_PASS",
                   chapter=chapter.number, findings=confirmed)
        return confirmed, suspects

    async def repair_findings(self, chapter, result, findings, request_meta=None):
        """One full-paragraph repair per ID; mutate accepted state only after validation."""
        raw_by_id = self._raw_by_id(chapter)
        by_id = defaultdict(list)
        for finding in findings:
            identifier = finding.get("id")
            if identifier not in raw_by_id:
                raise QualityError(f"Chapter {chapter.number} has broad/unlocalized QA failure: {finding}")
            by_id[identifier].append(finding)
        for identifier, issues in by_id.items():
            raw = raw_by_id[identifier]
            current = self._text_by_id(chapter, result, identifier)
            attempt_name = f"repair-attempts/{chapter.key}/{identifier}.json"
            attempt_fingerprint = digest({
                "raw": raw, "current": current, "dictionary": relevant_entries(self.dictionary, raw),
                "defects": issues, "policy": prompts.POLICY_VERSIONS["repair"],
            })
            previous_attempt = self.store.read(attempt_name)
            if previous_attempt and previous_attempt.get("fingerprint") == attempt_fingerprint:
                raise QualityError(f"Chapter {chapter.number} repair for {identifier} already failed; manual review required")
            payload = {
                "chapter": chapter.number, "id": identifier,
                "raw": raw, "current_vietnamese": current,
                "confirmed_defects": issues,
                "dictionary": relevant_entries(self.dictionary, raw),
            }
            self.event("REPAIR_REQUEST", chapter=chapter.number, paragraph_id=identifier,
                       defects=[item["kind"] for item in issues])
            try:
                repaired = await self.request("REPAIR_REQUEST", prompts.REPAIR,
                                              payload, Repair, chapter.number)
            except ProviderError as exc:
                if "Gemini blocked response:" not in str(exc):
                    raise
                self.store.write(f"validation-failures/{chapter.key}.json", {
                    "chapter": chapter.number, "final_status": "manual_review_required",
                    "paragraph_id": identifier, "findings": issues, "original_text": current,
                })
                raise QualityError(
                    f"Chapter {chapter.number} repair for {identifier} blocked by Gemini; "
                    "manual review required"
                ) from exc
            self.store.write(attempt_name, {"fingerprint": attempt_fingerprint,
                                            "status": "response_received"})
            if repaired.id != identifier:
                rejection = [{"kind": "repair_wrong_id", "expected": identifier,
                              "actual": repaired.id}]
            else:
                rejection = repair_integrity_findings(identifier, raw, current,
                                                      repaired.text, self.dictionary)
            if rejection:
                self.store.write(attempt_name, {"fingerprint": attempt_fingerprint,
                                                "status": "rejected", "findings": rejection})
                self.event("REPAIR_VALIDATION_FAIL", chapter=chapter.number,
                           paragraph_id=identifier, findings=rejection)
                self.event("REPAIR_REJECTED_KEEPING_ORIGINAL", chapter=chapter.number,
                           paragraph_id=identifier)
                self.store.write(f"validation-failures/{chapter.key}.json", {
                    "chapter": chapter.number, "final_status": "manual_review_required",
                    "paragraph_id": identifier, "findings": rejection,
                    "original_text": current,
                })
                raise QualityError(f"Chapter {chapter.number} repair for {identifier} was incomplete; original preserved")
            candidate = copy.deepcopy(result)
            self._set_text_by_id(chapter, candidate, identifier, repaired.text.strip())
            remaining_for_id = [finding for finding in self._local_findings(chapter, candidate)
                                if finding.get("id") == identifier and finding["kind"]
                                not in ("suspicious_length_ratio",)]
            if remaining_for_id:
                self.store.write(attempt_name, {"fingerprint": attempt_fingerprint,
                                                "status": "rejected", "findings": remaining_for_id})
                self.event("REPAIR_VALIDATION_FAIL", chapter=chapter.number,
                           paragraph_id=identifier, findings=remaining_for_id)
                self.event("REPAIR_REJECTED_KEEPING_ORIGINAL", chapter=chapter.number,
                           paragraph_id=identifier)
                raise QualityError(f"Chapter {chapter.number} repair for {identifier} failed validation")
            history_name = f"revisions/{chapter.key}/{identifier}.json"
            history = self.store.read(history_name, []) or []
            if not history:
                history.append({"revision": 0, "text": current, "kind": "initial"})
            history.append({"revision": len(history), "text": repaired.text.strip(),
                            "kind": "repair", "model": getattr(repaired, "_request_meta", {}).get("actual_model")})
            self.store.write(history_name, history)
            self._set_text_by_id(chapter, result, identifier, repaired.text.strip())
            self.store.write(attempt_name, {"fingerprint": attempt_fingerprint,
                                            "status": "accepted", "revision": len(history) - 1})
            self.event("REPAIR_VALIDATION_PASS", chapter=chapter.number,
                       paragraph_id=identifier,
                       actual_model=getattr(repaired, "_request_meta", {}).get("actual_model"))
            self.store.write(f"pending-chapters/{chapter.key}.json", {
                "raw_hash": digest((chapter.title, chapter.paragraphs)),
                "pre_dictionary_hash": self.pre_hash,
                "state": "validated_repair_pending_commit",
                "translation": result.model_dump(),
                "request_metadata": request_meta or [],
            })
            self.event("CHECKPOINT_SAVED", chapter=chapter.number, stage="accepted_repair")

    async def _validate_and_repair(self, chapter, result, request_meta):
        findings = self._local_findings(chapter, result)
        structural = [finding for finding in findings if finding["kind"] in
                      ("duplicate_id", "missing_id", "unexpected_id", "out_of_order_id", "empty_segment")]
        if structural:
            raise QualityError(f"Chapter {chapter.number} has structural defects: {structural}")
        hard = deterministic_defects(findings)
        suspicion = semantic_suspicions(findings)
        audited_ids = []
        confirmed = []
        if suspicion:
            confirmed, audited_ids = await self._audit_suspicions(chapter, result, suspicion)
        defects = hard + confirmed
        if defects:
            self.event("TRANSLATION_VALIDATION_FAIL", chapter=chapter.number, findings=defects)
            await self.repair_findings(chapter, result, defects, request_meta)
        remaining = self._local_findings(chapter, result)
        remaining = [finding for finding in remaining
                     if not (finding["kind"] in SEMANTIC_SUSPICIONS
                             and finding.get("id") in audited_ids
                             and finding.get("id") not in {item["id"] for item in confirmed})]
        if remaining:
            self.store.write(f"validation-failures/{chapter.key}.json", {
                "chapter": chapter.number, "final_status": "manual_review_required",
                "findings": remaining,
            })
            raise QualityError(f"Chapter {chapter.number} failed final local QA: {remaining[:3]}")
        self.event("TRANSLATION_VALIDATION_PASS", chapter=chapter.number)
        return audited_ids

    async def translate_chapter(self, chapter, previous_context=""):
        existing = self._valid_checkpoint(chapter)
        if existing:
            self.event("CHECKPOINT_SAVED", chapter=chapter.number, reused=True)
            return existing
        self.progress(f"Translating Chapter {chapter.number}", current_chapter=chapter.number)
        pending = self.store.read(f"pending-chapters/{chapter.key}.json")
        request_meta = []
        if (pending and pending.get("raw_hash") == digest((chapter.title, chapter.paragraphs))
                and pending.get("pre_dictionary_hash") == self.pre_hash
                and pending.get("state") in ("generated_unvalidated", "validated_repair_pending_commit")):
            result = Translation.model_validate(pending["translation"])
            request_meta = pending.get("request_metadata", [])
            self.event("CHECKPOINT_SAVED", chapter=chapter.number, reused_pending=True)
        else:
            limit = int(os.getenv("TRANSLATION_MAX_RAW_CHARS", "9000"))
            output_token_limit = int(os.getenv("TRANSLATION_MAX_ESTIMATED_OUTPUT_TOKENS", "12000"))
            if limit < 500:
                raise ValueError("TRANSLATION_MAX_RAW_CHARS must be at least 500")
            if output_token_limit < 1000:
                raise ValueError("TRANSLATION_MAX_ESTIMATED_OUTPUT_TOKENS must be at least 1000")
            units = _units(chapter, min(limit, max(500, output_token_limit // 2)))
            groups = _groups(units, limit, output_token_limit=output_token_limit)
            by_original = defaultdict(list)
            title = ""
            tail = [previous_context] if previous_context else []
            request_meta = []
            for group_number, group in enumerate(groups):
                next_source = groups[group_number + 1][0][1] if group_number + 1 < len(groups) else ""
                responses = await self._translate_group_with_recovery(
                    chapter, group, group_number, tail, next_source, group_number == 0)
                for response, meta, locally_accepted in responses:
                    request_meta.append(meta)
                    if group_number == 0 and not title:
                        title = response.title
                    for segment in response.segments:
                        original_id = segment.id.split(".F", 1)[0]
                        by_original[original_id].append(segment.text.strip())
                    if locally_accepted:
                        tail = (tail + [segment.text for segment in response.segments])[-4:]
            result = Translation(
                title=title,
                segments=[TranslatedSegment(id=identifier,
                                            text=" ".join(by_original[identifier]))
                          for identifier in chapter.paragraph_ids],
            )
            self.store.write(f"pending-chapters/{chapter.key}.json", {
                "raw_hash": digest((chapter.title, chapter.paragraphs)),
                "pre_dictionary_hash": self.pre_hash,
                "state": "generated_unvalidated",
                "translation": result.model_dump(),
                "request_metadata": request_meta,
            })
            self.event("CHECKPOINT_SAVED", chapter=chapter.number, stage="generated_unvalidated")
        audited_ids = await self._validate_and_repair(chapter, result, request_meta)
        manual_ids = accepted_review_ids(self.store, chapter, result, self.dictionary)
        self.store.write(f"chapters/{chapter.key}.json", {
            "raw_hash": digest((chapter.title, chapter.paragraphs)),
            "pre_dictionary_hash": self.pre_hash,
            "translation": result.model_dump(),
            "request_metadata": request_meta,
            "qa_status": "manual_override" if manual_ids else "pass", "state": "committed",
            "pipeline_version": PIPELINE_VERSION,
            "audited_ids": audited_ids,
            "manual_review_ids": manual_ids,
        })
        (self.store.path / f"pending-chapters/{chapter.key}.json").unlink(missing_ok=True)
        self.event("CHECKPOINT_SAVED", chapter=chapter.number, stage="chapter_committed")
        self.event("CHAPTER_COMPLETED", chapter=chapter.number)
        self._remember_ratios(chapter, result)
        self.progress("Translating", current_chapter=None)
        return result

    async def post_dictionary(self):
        saved = self.store.read("post-dictionary.json")
        if saved and saved.get("input_hash") == self.input_hash and saved.get("pre_hash") == self.pre_hash:
            self.dictionary = load_dictionary(saved["dictionary"])
            self.unresolved = load_unresolved({"unresolved": saved.get("unresolved", [])})
            self.rejected = load_rejected({"rejected": saved.get("rejected", [])})
            return
        scanned = scan(self.chapters, self.dictionary, (), post=True)
        scanned_sources = {item["source"] for item in scanned}
        for item in self.unresolved:
            if item["source"] not in scanned_sources:
                observed = _observe_source(self.chapters, item["source"])
                if observed["occurrences"]:
                    scanned.append(observed)
        known = validate_dictionary(self.dictionary)
        rejected_sources = {item["source"] for item in self.rejected}
        unresolved = {item["source"]: item for item in self.unresolved}
        candidates = []
        for item in scanned:
            source = item["source"]
            if source in known or source in rejected_sources or candidate_filter_reason(item):
                continue
            old = unresolved.get(source)
            old_keys = {(value.get("chapter"), value.get("text")) for value in old.get("evidence", [])
                        if isinstance(value, dict)} if old else set()
            new_keys = {(value.get("chapter"), value.get("text")) for value in item.get("evidence", [])
                        if isinstance(value, dict)}
            merged = _unresolved_merge([old] if old else [], [item], self.chapters)[0]
            changed_occurrences = not old or merged["occurrences"] > old.get("occurrences", 0)
            changed_evidence = bool(new_keys - old_keys)
            improved_type = bool(old and old.get("possible_type") == "unknown"
                                 and merged["possible_type"] != "unknown")
            retry_failure = bool(old and old.get("last_error")
                                 and old.get("last_attempt_input_hash") != self.input_hash)
            self.unresolved = _unresolved_merge(
                [record for record in self.unresolved if record["source"] != source],
                [merged], self.chapters,
            )
            if changed_occurrences or changed_evidence or improved_type or retry_failure:
                candidates.append(merged)
        self.event("POST_SCAN_COMPLETED", candidates=len(candidates))
        partial = self.store.read("post-dictionary-progress.json")
        offset = 0
        if (partial and partial.get("input_hash") == self.input_hash
                and partial.get("pre_hash") == self.pre_hash):
            self.dictionary = load_dictionary(partial["dictionary"])
            self.unresolved = load_unresolved({"unresolved": partial.get("unresolved", [])})
            self.rejected = load_rejected({"rejected": partial.get("rejected", [])})
            offset = partial["offset"]
        if candidates:
            self.progress("Post-dictionary review", candidate_count=len(candidates))
            await self.resolve(candidates[offset:], "post", offset)
        else:
            self.event("POST_DICTIONARY_SKIPPED")
        self.store.write("post-dictionary.json", {
            "input_hash": self.input_hash, "pre_hash": self.pre_hash,
            "dictionary": self.dictionary, "unresolved": self.unresolved,
            "rejected": self.rejected,
        })
        self.store.write("unresolved.json", self.unresolved)
        self.store.write("rejected.json", self.rejected)
        self.event("CHECKPOINT_SAVED", stage="post_dictionary")

    def _chapter_metrics(self, chapter, translated, saved):
        stats = self.store.read(f"chapter-request-statistics/{chapter.key}.json", {}) or {}
        actual_models = sorted({model for bucket in stats.values()
                                for model in bucket.get("actual_models", [])})
        accepted_models = sorted({metadata.get("actual_model")
                                  for metadata in saved.get("request_metadata", [])
                                  if metadata.get("actual_model")})
        for identifier in (*chapter.paragraph_ids, chapter.title_id):
            history = self.store.read(f"revisions/{chapter.key}/{identifier}.json", []) or []
            if history and history[-1].get("model"):
                accepted_models = sorted(set(accepted_models) | {history[-1]["model"]})
        findings = self._local_findings(chapter, translated)
        return {
            "chapter": chapter.number,
            "raw_chars": sum(map(len, chapter.paragraphs)),
            "raw_paragraphs": len(chapter.paragraphs),
            "translation_chars": sum(len(item.text) for item in translated.segments),
            "translated_paragraphs": len(translated.segments),
            "translation_requests": stats.get("translation", {}).get("logical_calls", 0),
            "qa_requests": stats.get("qa", {}).get("logical_calls", 0),
            "repair_requests": stats.get("repair", {}).get("logical_calls", 0),
            "technical_retries": sum(bucket.get("technical_retries", 0) for bucket in stats.values()),
            "fallbacks": sum(bucket.get("fallbacks", 0) for bucket in stats.values()),
            "actual_models": actual_models,
            "accepted_models": accepted_models,
            "cjk_findings": sum(item["kind"] == "chinese_residue" for item in findings),
            "numeric_findings": sum(item["kind"] in ("numeric_mismatch", "suspicious_numeric_mismatch")
                                    for item in findings),
            "terminology_findings": sum(item["kind"] == "locked_term_missing" for item in findings),
            "final_status": saved.get("qa_status", "unknown"),
        }

    async def run(self):
        inputs = self.store.read("inputs.json")
        if not inputs or "raw" not in inputs:
            raise QualityError("Missing RAW input checkpoint")
        self.input_hash = digest({"raw": inputs["raw"], "dictionary": inputs.get("dictionary")})
        try:
            self.chapters = parse_chapters(inputs["raw"])
        except ChapterValidationError as exc:
            self.event("RAW_VALIDATION_FAILED", detail=exc.detail)
            raise
        note_policy = author_note_policy()
        saved_note_policy = self.store.read("author-note-policy.json")
        if saved_note_policy and saved_note_policy.get("policy") != note_policy:
            raise QualityError("Author-note policy changed for this checkpoint; start a new job")
        self.store.write("author-note-policy.json", {"policy": note_policy})
        previous_parsed = (self.store.read("legacy-parsed-chapters.json")
                           or self.store.read("parsed-chapters.json") or {})
        if "pairs" in previous_parsed:
            self.store.write("legacy-parsed-chapters.json", previous_parsed)
            self.legacy_raw = {
                str(pair[0]["number"]): pair[0] for pair in previous_parsed["pairs"]
                if pair and isinstance(pair[0], dict) and "number" in pair[0]
            }
        self.store.write("parsed-chapters.json", {
            "input_hash": self.input_hash, "parser_version": PARSER_VERSION,
            "chapters": [{"number": chapter.number, "title": chapter.title,
                          "source_line": chapter.source_line,
                          "paragraph_ids": list(chapter.paragraph_ids),
                          "paragraphs": len(chapter.paragraphs)}
                         for chapter in self.chapters],
        })
        source_findings = source_quality_findings(self.chapters)
        self.store.write("source-quality.json", source_findings)
        if source_findings:
            self.event("SOURCE_QUALITY_FINDING", count=len(source_findings))
        self.event("JOB_STARTED", chapters=len(self.chapters))
        self.event("RAW_PARSED", chapters=len(self.chapters))
        self.event("CHECKPOINT_SAVED", stage="raw_parsed")
        self.progress("Parsing RAW")
        await self.pre_dictionary(inputs)
        self.progress("Translating")
        previous = ""
        width = self.scheduler.concurrency
        for start in range(0, len(self.chapters), width):
            wave = self.chapters[start:start + width]
            baseline = (tuple(self.ratio_baseline), tuple(self.paragraph_ratio_baseline))
            for chapter in wave:
                self.validation_baselines[chapter.number] = baseline
            results = await asyncio.gather(*(
                self.translate_chapter(chapter, previous if index == 0 else "")
                for index, chapter in enumerate(wave)
            ), return_exceptions=True)
            errors = [result for result in results if isinstance(result, BaseException)]
            if errors:
                raise errors[0]
            previous = "\n".join(segment.text for segment in results[-1].segments)[-600:]
        await self.post_dictionary()
        translations = []
        separate_notes = []
        for chapter in self.chapters:
            saved = self.store.read(f"chapters/{chapter.key}.json")
            if not saved or saved.get("qa_status") not in ("pass", "manual_override"):
                raise QualityError(f"Chapter {chapter.number} has no accepted checkpoint")
            translated = Translation.model_validate(saved["translation"])
            findings = self._local_findings(chapter, translated)
            audited = set(saved.get("audited_ids", []))
            findings = [item for item in findings
                        if not (item["kind"] in SEMANTIC_SUSPICIONS
                                and item.get("id") in audited)]
            if findings:
                self.event("TRANSLATION_VALIDATION_FAIL", chapter=chapter.number,
                           stage="post_dictionary", findings=findings)
                new_audited = await self._validate_and_repair(
                    chapter, translated, saved.get("request_metadata", []))
                saved["translation"] = translated.model_dump()
                saved["audited_ids"] = sorted(audited.union(new_audited))
            manual_ids = accepted_review_ids(self.store, chapter, translated, self.dictionary)
            qa_status = "manual_override" if manual_ids else "pass"
            if (saved.get("manual_review_ids") != manual_ids
                    or saved.get("qa_status") != qa_status or findings):
                saved["manual_review_ids"] = manual_ids
                saved["qa_status"] = qa_status
                self.store.write(f"chapters/{chapter.key}.json", saved)
                if findings:
                    self.event("CHECKPOINT_SAVED", chapter=chapter.number,
                               stage="post_dictionary_repair")
            metrics = self._chapter_metrics(chapter, translated, saved)
            self.store.write(f"metrics/{chapter.key}.json", metrics)
            note_ids = [identifier for identifier, raw in chapter.paragraph_items
                        if is_author_note(raw)]
            reader_paragraphs = [item.text for item in translated.segments
                                 if note_policy == "preserve" or item.id not in note_ids]
            if note_policy == "separate" and note_ids:
                separate_notes.append({
                    "number": chapter.number,
                    "notes": [item.text for item in translated.segments if item.id in note_ids],
                })
            translations.append({
                "number": chapter.number, "title": translated.title,
                "segments": [item.model_dump() for item in translated.segments],
                "paragraphs": reader_paragraphs,
                "author_note_ids": note_ids,
                "qa_status": saved["qa_status"], "metrics": metrics,
            })
        output = "\n\n".join(
            f"Chương {item['number']}" + (f": {item['title']}" if item["title"] else "")
            + "\n" + "\n\n".join(item["paragraphs"])
            for item in translations
        )
        self.store.write("translated.json", {"chapters": translations})
        self.store.write("translated.txt", output + "\n")
        if note_policy == "separate":
            note_output = "\n\n".join(
                f"Chương {item['number']} — Ghi chú tác giả\n" + "\n\n".join(item["notes"])
                for item in separate_notes
            )
            self.store.write("author-notes.txt", note_output + "\n" if note_output else "")
        self.store.write("dictionary.json", {
            "version": self.dictionary["version"],
            "entries": self.dictionary["entries"],
            "unresolved": self.unresolved,
            "rejected": self.rejected,
        })
        self.store.write("unresolved.json", self.unresolved)
        self.store.write("rejected.json", self.rejected)
        self.store.write("pipeline-version.json", {"version": PIPELINE_VERSION})
        self.event("CHECKPOINT_SAVED", stage="batch_complete")
        self.event("BATCH_COMPLETED", chapters=len(self.chapters),
                   dictionary_entries=len(self.dictionary["entries"]))
        self.store.progress(
            "done", stage="Complete", total_chapters=len(self.chapters),
            completed_chapters=len(self.chapters), chapters_finalized=len(self.chapters),
            confirmed_terms=len(self.dictionary["entries"]), unresolved_terms=len(self.unresolved),
            dictionary_hash=digest(self.dictionary), request_statistics=self.store.request_statistics(),
            error=None, error_detail=None,
        )
