"""Strict public and provider schemas for the RAW-only translation pipeline."""

from __future__ import annotations

from typing import List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field

MODELS = ("gemini-3.1-flash-lite", "gemini-3.5-flash-lite")
PRIMARY_MODEL, FALLBACK_MODEL = MODELS

# These versions are part of job/cache identity.  Bump them whenever a persisted
# semantic contract changes; old accepted chapters are migrated explicitly.
PIPELINE_VERSION = 23
PARSER_VERSION = 8
DICTIONARY_VERSION = 10

DictionaryType = Literal[
    "character",
    "location",
    "institution",
    "book_title",
    "memorial",
    "book_section",
    "term",
]
Gender = Literal["male", "female", "unknown", "not_applicable"]

class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

class Inputs(StrictModel):
    raw: str = Field(min_length=1, max_length=20_000_000)
    dictionary: Optional[Union[dict, list]] = None
    book_title: Optional[str] = Field(default=None, max_length=500)
    source_name: Optional[str] = Field(default=None, max_length=500)

class Alias(StrictModel):
    source: str = Field(min_length=1)
    translation: str = Field(min_length=1)

class Entry(StrictModel):
    source: str = Field(min_length=1)
    translation: str = Field(min_length=1)
    type: DictionaryType
    gender: Gender
    status: Literal["locked"]
    aliases: List[Alias] = Field(default_factory=list)

class AddEntry(StrictModel):
    operation: Literal["add_entry"]
    entry: Entry

class AddAlias(StrictModel):
    operation: Literal["add_alias"]
    canonical_source: str = Field(min_length=1)
    alias: Alias

class Unresolved(StrictModel):
    source: str = Field(min_length=1)
    possible_type: Literal["character", "location", "institution", "book_title", "term", "unknown"] = "unknown"
    reason: str = "insufficient evidence"
    evidence: List[str] = Field(default_factory=list)

class Rejected(StrictModel):
    source: str = Field(min_length=1)
    reason: str = Field(min_length=1)

class DictionaryPatch(StrictModel):
    confirmed: List[Union[AddEntry, AddAlias]] = Field(default_factory=list)
    rejected: List[Rejected] = Field(default_factory=list)
    unresolved: List[Unresolved] = Field(default_factory=list)

class ProposedPatch(StrictModel):
    """Provider envelope; each proposed operation is validated locally."""
    confirmed: List[Union[AddEntry, AddAlias]] = Field(default_factory=list)
    rejected: List[Rejected] = Field(default_factory=list)
    unresolved: List[Unresolved] = Field(default_factory=list)

class TranslatedSegment(StrictModel):
    id: str = Field(pattern=r"^P\d{4,}_\d{4}(?:\.F\d{4})*$")
    text: str = Field(min_length=1)

class Translation(StrictModel):
    title: str
    segments: List[TranslatedSegment]

class Repair(StrictModel):
    id: str = Field(min_length=1)
    text: str = Field(min_length=1)


class ConfirmedDefect(StrictModel):
    id: str = Field(min_length=1)
    kind: Literal[
        "omission",
        "mistranslation",
        "reversed_meaning",
        "negation",
        "numeric_value",
        "wrong_speaker",
        "wrong_subject_object",
        "hallucination",
        "untranslated_source",
        "locked_terminology",
        "truncation",
    ]
    reason: str = Field(min_length=1)

class CoverageAudit(StrictModel):
    findings: List[ConfirmedDefect] = Field(default_factory=list)
