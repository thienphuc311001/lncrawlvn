from __future__ import annotations

from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

MODELS = ("gemini-3.1-flash-lite", "gemini-3.5-flash-lite", "gemini-3.7-flash")
PIPELINE_VERSION = 1


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


class Resolution(StrictModel):
    decision: Literal["ACCEPT", "REVIEW", "REJECT"]
    term: Optional[Term] = None
    reason: str


class Evidence(StrictModel):
    findings: str = Field(max_length=3000)


class Context(StrictModel):
    context: str = Field(max_length=6000)


class Segment(StrictModel):
    id: int
    text: str = Field(min_length=1)


class Translation(StrictModel):
    title: str
    segments: List[Segment]


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
