from __future__ import annotations

from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

MODELS = ("gemini-3.1-flash-lite", "gemini-3.5-flash-lite", "gemini-3.7-flash")
PIPELINE_VERSION = 5
PARSER_VERSION = 5


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Inputs(StrictModel):
    raw: str = Field(min_length=1, max_length=20_000_000)
    vietphrase: str = Field(min_length=1, max_length=40_000_000)
    dictionary: Optional[dict] = None


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
    ]
    status: Literal["locked", "provisional"] = "provisional"
    gender: Literal["male", "female", "unknown"] = "unknown"
    aliases: List[str] = Field(default_factory=list)
    forms: Dict[str, str] = Field(default_factory=dict)
    evidence: str = ""


class Eligibility(StrictModel):
    complete_semantic_unit: bool
    named_or_novel_specific: bool
    consistency_matters: bool
    evidence_supports: bool


class Resolution(StrictModel):
    decision: Literal["ACCEPT", "REVIEW", "REJECT"]
    eligibility: Eligibility
    term: Optional[Term] = None
    reason: str


class CandidateResolution(Resolution):
    source: str


class BatchResolution(StrictModel):
    results: List[CandidateResolution]

    @field_validator("results", mode="before")
    @classmethod
    def preserve_valid_independent_results(cls, value):
        if not isinstance(value, list):
            raise ValueError("Resolver results must be an array")
        valid = []
        for record in value:
            try:
                valid.append(CandidateResolution.model_validate(record))
            except (ValueError, TypeError):
                # The pipeline knows every expected source and retries only
                # missing/invalid candidates, retaining independent valid work.
                continue
        return valid


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


class Validation(StrictModel):
    issues: List[Issue]
    missed_terms: List[Candidate]


class Repair(StrictModel):
    segments: List[Segment]
