from __future__ import annotations

import os
from contextvars import ContextVar
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

from .style import AUTO, MODERN, SINO_VIETNAMESE, configured_register

MODELS = ("gemini-3.1-flash-lite", "gemini-3.5-flash-lite", "gemini-3.7-flash")
PIPELINE_VERSION = 8
DICTIONARY_VERSION = 4
PARSER_VERSION = 5
CONFIRMED = "CONFIRMED"
IGNORE = "IGNORE"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Inputs(StrictModel):
    raw: str = Field(min_length=1, max_length=20_000_000)
    vietphrase: str = Field(min_length=1, max_length=40_000_000)
    dictionary: Optional[dict] = None


class ResolutionPolicy(StrictModel):
    min_contexts: int = Field(default=3, ge=1, le=100)
    min_dominance: float = Field(default=0.95, ge=0.5, le=1.0)
    min_coverage: float = Field(default=0.95, ge=0.5, le=1.0)
    # Address/title register.  Sino-Vietnamese is the project default, so the
    # resolver may not silently drift into modern conversational wording.
    address_register: Literal[SINO_VIETNAMESE, MODERN, AUTO] = SINO_VIETNAMESE
    min_register_evidence: int = Field(default=2, ge=1, le=1000)
    min_register_dominance: float = Field(default=0.6, ge=0.5, le=1.0)

    @classmethod
    def from_environment(cls):
        def number(name, default):
            value = os.getenv(name)
            return default if value is None else value

        return cls(
            min_contexts=number("TRANSLATION_DICTIONARY_MIN_CONTEXTS", 3),
            min_dominance=number("TRANSLATION_DICTIONARY_MIN_DOMINANCE", 0.95),
            min_coverage=number("TRANSLATION_DICTIONARY_MIN_COVERAGE", 0.95),
            address_register=configured_register(),
            min_register_evidence=number("TRANSLATION_DICTIONARY_REGISTER_MIN_EVIDENCE", 2),
            min_register_dominance=number("TRANSLATION_DICTIONARY_REGISTER_MIN_DOMINANCE", 0.6),
        )


class Chapter(StrictModel):
    number: int
    title: str
    paragraphs: List[str]
    paragraph_lines: List[int] = Field(default_factory=list)
    blank_breaks: List[bool] = Field(default_factory=list)
    volume: Optional[int] = None
    source_line: Optional[int] = Field(default=None, exclude=True)
    input_label: Optional[str] = Field(default=None, exclude=True)
    previous_number: Optional[int] = Field(default=None, exclude=True)
    previous_line: Optional[int] = Field(default=None, exclude=True)
    meaningful_text: Optional[str] = Field(default=None, exclude=True)

    @property
    def key(self) -> str:
        return f"v{self.volume}-c{self.number}" if self.volume is not None else str(self.number)


class AlignmentGroup(StrictModel):
    raw: List[int] = Field(min_length=1)
    vp: List[int] = Field(min_length=1)
    safe_break: bool


class Alignment(StrictModel):
    confirmed: bool
    groups: List[AlignmentGroup]


class Candidate(StrictModel):
    source: str
    evidence: str


class Candidates(StrictModel):
    candidates: List[Candidate]


class Term(StrictModel):
    source: str
    translation: str
    type: Literal[
        "character",
        "character_form",
        "character_alias",
        "title",
        "occupation",
        "faction",
        "organization",
        "location",
        "realm",
        "technique",
        "artifact",
        "weapon",
        "race",
        "creature",
        "concept",
        "other_term",
        "unknown",
        "proper_noun",
        "generic_phrase",
        "common_noun",
        "verb_phrase",
        "descriptive_phrase",
    ]
    status: Literal[
        "locked",
        "provisional",
        "needs_review",
        "report_only",
        "unresolved",
    ] = "provisional"
    gender: Literal["male", "female", "unknown"] = "unknown"
    aliases: List[str] = Field(default_factory=list)
    forms: Dict[str, str] = Field(default_factory=dict)
    # Address/title class of each form (kinship, honorific, official title,
    # rank, role, nickname, alias) and the register enforced for them, so a
    # frozen dictionary documents its own style instead of re-deriving it.
    form_kinds: Dict[str, str] = Field(default_factory=dict)
    address_register: Optional[Literal[SINO_VIETNAMESE, MODERN]] = None
    evidence: str = ""
    semantic_resolution: Literal["resolved", "unresolved"] = "resolved"
    needs_review: bool = False
    resolution_reason: Optional[str] = None
    confidence: Optional[float] = None
    resolver_source: Optional[str] = None
    fallback: Optional[str] = None
    # This is intentionally explicit.  A Vietnamese suggestion is not, by
    # itself, a frozen terminology constraint.  Missing legacy values are
    # migrated conservatively by the validator below.
    enforceable: bool = False
    # Runtime deliberately has only two decisions.  The historical status and
    # diagnostic fields above are retained solely so old checkpoints can be
    # read; translation-time code uses this value and never enforces IGNORE.
    runtime_state: Literal["CONFIRMED", "IGNORE"] = IGNORE

    @model_validator(mode="before")
    @classmethod
    def migrate_enforceability(cls, value):
        if not isinstance(value, dict):
            return value
        value = dict(value)
        if value.get("status") in {"confirmed", "CONFIRMED"}:
            value["status"] = "locked"
            value["enforceable"] = True
        elif value.get("status") in {"ignore", "IGNORE"}:
            value["status"] = "provisional"
            value["enforceable"] = False
        if "enforceable" not in value:
            value["enforceable"] = value.get("status") == "locked"
        if value.get("status") in {"report_only", "needs_review", "unresolved"}:
            value["enforceable"] = False
        if value.get("status") == "locked":
            value["enforceable"] = True
        return value

    @model_validator(mode="after")
    def normalize_enforceability(self):
        if self.status == "locked":
            self.enforceable = True
        elif self.status in {"report_only", "needs_review", "unresolved"}:
            self.enforceable = False
        if self.status != "locked":
            self.enforceable = False
        self.runtime_state = CONFIRMED if self.enforceable else IGNORE
        return self


class ResolverTerm(StrictModel):
    """Provider-facing term; uncertain metadata is normalized before export."""

    source: str
    translation: str
    type: str
    status: Literal["locked", "provisional"] = "provisional"
    gender: str = "unknown"
    aliases: List[str] = Field(default_factory=list)
    forms: Dict[str, str] = Field(default_factory=dict)
    form_kinds: Dict[str, str] = Field(default_factory=dict)
    evidence: str = ""
    semantic_resolution: Literal["resolved", "unresolved"] = "resolved"
    needs_review: bool = False
    resolution_reason: Optional[str] = None
    enforceable: bool = False
    confidence: Optional[float] = None
    resolver_source: Optional[str] = None
    fallback: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def normalize_uncertain_metadata(cls, value):
        if not isinstance(value, dict):
            return value
        value = dict(value)
        if str(value.get("status", "")).casefold() == "confirmed":
            value["status"] = "locked"
        elif str(value.get("status", "")).casefold() == "ignore":
            value["status"] = "provisional"
        allowed = set(Term.model_fields["type"].annotation.__args__)
        if value.get("type") not in allowed:
            value["type"] = "unknown"
            value["semantic_resolution"] = "unresolved"
            value["needs_review"] = True
            value.setdefault("resolution_reason", "semantic_type_ambiguous")
        if value.get("gender") not in {"male", "female", "unknown"}:
            value["gender"] = "unknown"
            value["semantic_resolution"] = "unresolved"
            value["needs_review"] = True
            value.setdefault("resolution_reason", "semantic_gender_ambiguous")
        if value.get("status") == "locked":
            value["enforceable"] = True
        if (
            value.get("needs_review")
            or value.get("semantic_resolution") == "unresolved"
            or (
                value.get("confidence") is not None
                and isinstance(value.get("confidence"), (int, float))
                and value.get("confidence") < 0.8
                and value.get("status") != "locked"
            )
        ):
            value["enforceable"] = False
        return value


class Eligibility(StrictModel):
    complete_semantic_unit: bool
    named_or_novel_specific: bool
    consistency_matters: bool
    evidence_supports: bool


class Resolution(StrictModel):
    decision: Literal["ACCEPT", "REVIEW", "REJECT"]
    eligibility: Eligibility
    term: Optional[ResolverTerm] = None
    reason: str


class CandidateResolution(Resolution):
    source: str


_INVALID_BATCH_RESULTS = ContextVar("invalid_batch_results", default=[])


class BatchResolution(StrictModel):
    results: List[CandidateResolution]
    _raw_results: List[dict] = PrivateAttr(default_factory=list)

    @property
    def raw_results(self):
        return self._raw_results

    @model_validator(mode="before")
    @classmethod
    def preserve_valid_independent_results(cls, value):
        if not isinstance(value, dict) or not isinstance(value.get("results"), list):
            raise ValueError("Resolver results must be an array")
        valid = []
        invalid = []
        for record in value["results"]:
            try:
                valid.append(CandidateResolution.model_validate(record))
            except (ValueError, TypeError):
                # The pipeline knows every expected source and retries only
                # missing/invalid candidates, retaining independent valid work.
                if isinstance(record, dict):
                    invalid.append(record)
        _INVALID_BATCH_RESULTS.set(invalid)
        return {"results": valid}

    @model_validator(mode="after")
    def collect_invalid_results(self):
        self._raw_results = _INVALID_BATCH_RESULTS.get()
        _INVALID_BATCH_RESULTS.set([])
        return self


class Evidence(StrictModel):
    findings: str = Field(max_length=3000)


class Context(StrictModel):
    context: str = Field(max_length=6000)


class Segment(StrictModel):
    id: int
    # Empty text is a concrete, repairable local finding, not a provider/schema
    # failure that discards otherwise valid paragraphs.
    text: str


class Translation(StrictModel):
    title: str
    segments: List[Segment]
    unresolved_terms: List[Candidate] = Field(default_factory=list)


class Issue(StrictModel):
    segment_id: int
    kind: Literal[
        "missing",
        "invented",
        "speaker",
        "subject",
        "object",
        "pronoun",
        "negation",
        "number",
        "name",
        "terminology",
        "duplicate",
        "dialogue",
        "order",
    ]
    explanation: str
    # Structured validator data is optional for non-terminology findings so
    # old checkpoints and callers remain valid.
    type: Optional[str] = None
    source: Optional[str] = None
    matched_source: Optional[str] = None
    canonical_source: Optional[str] = None
    required_translation: Optional[str] = None
    actual_text: Optional[str] = None
    reason: Optional[str] = None
    start: Optional[int] = None
    end: Optional[int] = None
    left_context: Optional[str] = None
    right_context: Optional[str] = None
    context: Optional[str] = None
    source_spans: Optional[List[Dict[str, object]]] = None
    source_occurrences: Optional[int] = None
    expected_occurrences: Optional[int] = None
    matched_occurrences: Optional[int] = None
    location: Optional[Dict[str, object]] = None

    @model_validator(mode="after")
    def normalize_type(self):
        if self.type is None:
            self.type = self.kind
        return self


class Validation(StrictModel):
    issues: List[Issue]
    missed_terms: List[Candidate]


class Repair(StrictModel):
    segments: List[Segment]
