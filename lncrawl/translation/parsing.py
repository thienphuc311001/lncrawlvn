"""Deterministic RAW chapter parser with source-line diagnostics."""
from __future__ import annotations
import re
from dataclasses import dataclass

CHINESE_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
                  "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
CHINESE_UNITS = {"十": 10, "百": 100, "千": 1_000, "万": 10_000, "亿": 100_000_000}
CHINESE_NUMBER = r"零〇一二三四五六七八九十百千万亿两"
HEADING = re.compile(rf"^\s*第\s*(\d+|[{CHINESE_NUMBER}]+)\s*章\s*(.*)\s*$")
HEADING_LIKE = re.compile(r"^\s*第\s*([^\s章]+)\s*章")
REFERENCE = re.compile(r"^(?:中|里|里面)(?:记载|提到|描述|说过|写道)|^(?:所述|提到|说过)")
SEPARATOR = re.compile(r"-{20,}")

INLINE_SENTENCE_PUNCTUATION = re.compile(r"[，。！？；：,.!?;:]")

def split_inline_body(title: str) -> tuple[str, str | None]:
    parts = title.split(maxsplit=1)
    if len(parts) == 2 and INLINE_SENTENCE_PUNCTUATION.search(parts[1]):
        return parts[0], parts[1]
    return title, None


def chapter_number(value: str) -> int:
    if value.isdecimal():
        return int(value)
    total = section = number = 0
    for character in value:
        if character in CHINESE_DIGITS:
            number = CHINESE_DIGITS[character]
            continue
        unit = CHINESE_UNITS[character]
        if unit < 10_000:
            section += (number or 1) * unit
            number = 0
        else:
            section += number
            total += (section or 1) * unit
            section = number = 0
    return total + section + number

class ChapterValidationError(ValueError):
    def __init__(self, error_type, chapter=None, line=None, previous_line=None, message=None,
                 input_name="RAW", reason=None):
        self.detail = {
            "error_type": error_type,
            "input": input_name,
            "chapter": chapter,
            "line": line,
            "previous_line": previous_line,
            "reason": reason or message or error_type.replace("_", " "),
            "message": message or error_type.replace("_", " "),
        }
        super().__init__(self.detail["message"])

@dataclass(frozen=True)
class Chapter:
    number: int
    title: str
    paragraphs: tuple[str, ...]
    source_line: int
    paragraph_lines: tuple[int, ...]

    @property
    def paragraph_ids(self):
        return tuple(f"P{self.number:04d}_{index:04d}" for index in range(1, len(self.paragraphs) + 1))

    @property
    def title_id(self):
        return f"C{self.number:04d}_TITLE"

    @property
    def paragraph_items(self):
        return tuple(zip(self.paragraph_ids, self.paragraphs))

    @property
    def key(self):
        return str(self.number)

    @property
    def raw(self):
        return "\n".join(self.paragraphs)

def parse_chapters(text: str, label: str = "RAW", diagnostics=None) -> list[Chapter]:
    if not isinstance(text, str) or not text.strip():
        raise ChapterValidationError("empty_raw", message="RAW text is empty", input_name=label)
    lines = text.removeprefix("\ufeff").splitlines()
    chapters = []
    current = None
    paragraphs = []
    paragraph_lines = []
    seen = {}
    for line_number, original in enumerate(lines, 1):
        line = original.strip()
        if "\ufffd" in original or "\x00" in original:
            raise ChapterValidationError("invalid_encoding", line=line_number, input_name=label)
        if SEPARATOR.fullmatch(line):
            continue
        heading = HEADING.fullmatch(line)
        body_reference = bool(heading and REFERENCE.match(heading[2]))
        if body_reference:
            heading = None
        if not heading and not body_reference and HEADING_LIKE.match(line):
            raise ChapterValidationError(
                "malformed_heading", line=line_number, input_name=label,
                reason=f"Heading-like line is not a valid numbered chapter: {line[:120]}",
            )
        if heading:
            number = chapter_number(heading[1])
            if number in seen:
                raise ChapterValidationError("duplicate_chapter", number, line_number, seen[number], input_name=label)
            if current is not None:
                if not paragraphs:
                    raise ChapterValidationError("empty_chapter", current[0], current[2], input_name=label)
                chapters.append(Chapter(current[0], current[1], tuple(paragraphs), current[2], tuple(paragraph_lines)))
            if chapters and number < chapters[-1].number:
                raise ChapterValidationError("backwards_chapter", number, line_number, chapters[-1].source_line,
                                             input_name=label)
            title, inline_body = split_inline_body(heading[2].strip())
            current = (number, title, line_number)
            seen[number] = line_number
            paragraphs, paragraph_lines = [], []
            if inline_body:
                paragraphs.append(inline_body)
                paragraph_lines.append(line_number)
        elif line:
            if current is None:
                raise ChapterValidationError("content_before_chapter", line=line_number, input_name=label)
            paragraphs.append(original.strip())
            paragraph_lines.append(line_number)
    if current is None:
        raise ChapterValidationError("no_chapters", message="No 第N章 headings found", input_name=label)
    if not paragraphs:
        raise ChapterValidationError("empty_chapter", current[0], current[2], input_name=label)
    chapters.append(Chapter(current[0], current[1], tuple(paragraphs), current[2], tuple(paragraph_lines)))
    return chapters

def validate_inputs(raw: str) -> list[Chapter]:
    return parse_chapters(raw)
