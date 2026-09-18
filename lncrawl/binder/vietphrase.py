"""VietPhrase-safe TXT layout for exported chapters.

VietPhrase.app joins adjacent source lines around an internal chunk boundary of
roughly 4000 characters, which loses physical line boundaries in the converted
file. Exporting each chapter as contiguous blocks that stay well below that
boundary — separated by one blank line — keeps every crawled line on its own
physical line inside the app's chunks.

Chunking is deterministic and local: it uses only structural signals (source
paragraph boundaries, Chinese sentence punctuation, quotation state and block
length). There are no model calls, no summarization and no network access, so a
resumed crawl and a fresh crawl produce identical files.
"""

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ..exceptions import LNException

logger = logging.getLogger(__name__)

__all__ = [
    "CHAPTER_SEPARATOR",
    "FRONT_MATTER_MARKER",
    "SAFE_BLOCK_MAX",
    "SAFE_BLOCK_SLACK",
    "SAFE_BLOCK_TARGET",
    "ChapterBlocks",
    "ExportText",
    "build_chapter_blocks",
    "build_export_text",
    "content_lines",
    "normalize_newlines",
    "resolve_safe_block_limits",
    "validate_export",
]

# Targets for block size in Chinese/source characters. The target is where the
# exporter starts looking for a natural boundary; the maximum is a hard limit
# kept below the observed ~4000-character VietPhrase.app chunk boundary.
SAFE_BLOCK_TARGET = 2600
SAFE_BLOCK_MAX = 3000
# Pre-target slack: a clean paragraph/dialogue boundary at ~2350-2550 is
# preferred over a weaker one near the maximum.
SAFE_BLOCK_SLACK = 300
# A remainder shorter than this would become an obviously tiny trailing block.
SHORT_TAIL_CHARS = 200
# Soft scoring penalty only: never overrides a stronger structural boundary,
# never pushes a block past the maximum, never rebalances finalized blocks.
SHORT_TAIL_PENALTY = 30

TARGET_ENV = "VIETPHRASE_SAFE_BLOCK_TARGET"
MAX_ENV = "VIETPHRASE_SAFE_BLOCK_MAX"

CHAPTER_SEPARATOR = "-" * 60
FRONT_MATTER_MARKER = "+" * 60

# The synthetic wrapper this binder must never emit again.
WRAPPER_PATTERN = re.compile(r"^\s*chapter\s+\d+\s*[:：]", re.I)

# Boundary classes, strongest first. Class rank outranks any position bonus so
# a weaker boundary is never chosen while a stronger one is in reach.
_BOUNDARY_RANK = {
    "section": 5,
    "dialogue": 4,
    "paragraph": 4,
    "sentence": 3,
    "medium": 2,
    "weak": 1,
}
_BOUNDARY_SCORE = {
    "section": 120,
    "dialogue": 140,
    "paragraph": 100,
    "sentence": 80,
    "medium": 30,
    "weak": 0,
}

# position bonuses: inside the target window, and around the sweet spot
_WINDOW_BONUS = 30
_SWEET_SPOT_BONUS = 20
_SWEET_SPOT = 2800

QUOTATION_OPEN = "“‘「『"
QUOTATION_CLOSE = "”’」』"
QUOTE_PAIRS = (("“", "”"), ("‘", "’"), ("「", "」"), ("『", "』"))

_STRONG_STOPS = "。！？…"
_MEDIUM_STOPS = "；"
_WEAK_STOPS = "，：、"

_TITLE_BRACKETS = (("《", "》"), ("（", "）"), ("(", ")"), ("【", "】"), ("〈", "〉"))
_TRAILING_CLOSERS = "”’」』）》〉"


def normalize_newlines(text: str) -> str:
    """Return the text with deterministic LF line endings."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def content_lines(text: str) -> List[str]:
    """Physical source lines, verbatim, with whitespace-only lines dropped."""
    return [line for line in normalize_newlines(text).split("\n") if line.strip()]


def resolve_safe_block_limits(
    target: Optional[int] = None,
    maximum: Optional[int] = None,
) -> Tuple[int, int]:
    """Resolve block target and hard maximum.

    Precedence per value: explicit argument, ``ctx.config`` (web UI setting),
    environment variable, built-in default. The target is clamped to the
    maximum so a misconfigured pair can never make a boundary target exceed the
    hard limit.
    """
    resolved_max = _resolve_one(maximum, "safe_block_max", MAX_ENV, SAFE_BLOCK_MAX)
    resolved_target = _resolve_one(target, "safe_block_target", TARGET_ENV, SAFE_BLOCK_TARGET)
    if resolved_target > resolved_max:
        logger.warning(
            "safe block target %d exceeds the maximum %d; clamping the target",
            resolved_target,
            resolved_max,
        )
        resolved_target = resolved_max
    return resolved_target, resolved_max


def _resolve_one(value: Optional[int], attr: str, env: str, default: int) -> int:
    for candidate in (value, _config_value(attr), _env_value(env)):
        try:
            number = int(candidate)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if number > 0:
            return number
    return default


def _config_value(attr: str):
    try:
        from ..context import ctx

        return getattr(ctx.config, attr, None)
    except Exception:  # pragma: no cover - context is optional for pure helpers
        return None


def _env_value(env: str):
    return os.getenv(env)


@dataclass
class Boundary:
    """A candidate cut before ``source_lines[index]``."""

    index: int
    kind: str
    score: int
    block_chars: int
    next_chars: int
    inside_quotation: bool = False
    depth: int = 0

    @property
    def rank(self) -> int:
        return _BOUNDARY_RANK.get(self.kind, 0)


@dataclass
class ChapterBlocks:
    """Chunked body lines of one chapter plus its audit counters."""

    lines: List[str] = field(default_factory=list)
    blocks: List[List[str]] = field(default_factory=list)
    block_sizes: List[int] = field(default_factory=list)
    boundaries: List[Boundary] = field(default_factory=list)
    oversized_splits: int = 0
    oversized_segments: int = 0
    oversized_headings: int = 0
    oversized_heading_sizes: List[int] = field(default_factory=list)
    forced_splits: int = 0
    weak_fallbacks: int = 0
    open_quote_cuts: int = 0
    stunted_tail_cuts: int = 0

    @property
    def content(self) -> List[str]:
        """Written lines, without block blanks (oversized splits expand)."""
        if self.lines:
            return list(self.lines)
        return [line for block in self.blocks for line in block]


# --------------------------------------------------------------------------- #
# Structural state
# --------------------------------------------------------------------------- #



def quotation_depths(lines: Iterable[str]) -> List[int]:
    """Open-quotation depth after each line; index 0 is before the first line."""
    depths = [0]
    for line in lines:
        depths.append(_advance_depth(depths[-1], line))
    return depths


def bracket_depths(lines: Iterable[str]) -> List[int]:
    """Open ``《》``/``（）`` depth after each line."""
    depths = [0]
    for line in lines:
        depth = depths[-1]
        for char in line:
            for opener, closer in _TITLE_BRACKETS:
                if char == opener:
                    depth += 1
                elif char == closer and depth > 0:
                    depth -= 1
        depths.append(depth)
    return depths


def _advance_depth(depth: int, line: str) -> int:
    for char in line:
        if char in QUOTATION_OPEN:
            depth += 1
        elif char in QUOTATION_CLOSE and depth > 0:
            depth -= 1
    return depth


def _ending_class(line: str) -> str:
    """Punctuation class of a line's ending: strong, medium, weak or none."""
    stripped = line.rstrip()
    index = len(stripped) - 1
    while index >= 0 and stripped[index] in _TRAILING_CLOSERS:
        index -= 1
    if index < 0:
        return "none"
    char = stripped[index]
    if char in _STRONG_STOPS:
        return "strong"
    if char in _MEDIUM_STOPS:
        return "medium"
    if char in _WEAK_STOPS:
        return "weak"
    return "none"


def _is_speech_intro(previous: str, following: str) -> bool:
    """``说话人说道：`` directly followed by the quoted speech on the next line."""
    previous = previous.rstrip()
    if not previous or not previous.endswith("："):
        return False
    return following.lstrip()[:1] in QUOTATION_OPEN


_ENDING_ADJUST = {"strong": 0, "medium": -10, "weak": -20, "none": -30}
_SPEECH_INTRO_PENALTY = -60
_OPEN_QUOTE_PENALTY = -1000
_INSIDE_BRACKETS_PENALTY = -1000
_SWEET_SPOT_HALF = 100


def classify_boundary(
    previous_line: str,
    following_line: str,
    *,
    section: bool,
    closed_quotation: bool,
) -> str:
    """Boundary class for the cut between two complete source lines."""
    if section:
        return "section"
    if closed_quotation:
        return "dialogue"
    return "paragraph"


def score_boundary(
    kind: str,
    *,
    ending: str,
    block_chars: int,
    next_chars: int,
    speech_intro: bool,
    inside_quotation: bool,
    inside_brackets: bool,
    target: int,
    maximum: int,
) -> int:
    """Deterministic score for one candidate boundary.

    Class rank is compared first, so this score only orders candidates of the
    same class. The short-remainder penalty is intentionally soft: it can never
    promote a weaker class and never makes a block exceed the maximum.
    """
    score = _BOUNDARY_SCORE[kind] + _ENDING_ADJUST[ending]
    if speech_intro:
        score += _SPEECH_INTRO_PENALTY
    if target <= block_chars <= maximum:
        score += _WINDOW_BONUS
        if abs(block_chars - _SWEET_SPOT) <= _SWEET_SPOT_HALF:
            score += _SWEET_SPOT_BONUS
    if inside_quotation:
        score += _OPEN_QUOTE_PENALTY
    if inside_brackets:
        score += _INSIDE_BRACKETS_PENALTY
    if 0 < next_chars < SHORT_TAIL_CHARS:
        score += -SHORT_TAIL_PENALTY
    return score


def select_boundary(candidates: List[Boundary]) -> Optional[Boundary]:
    """Best candidate: strongest class, then score, then shallowest quotations.

    Class rank is compared first so a weaker boundary is never chosen while a
    stronger one is in reach. Among equal-rank, equal-score candidates the
    shallowest quotation depth wins, which avoids splitting a nested dialogue
    when a shallower (or closed) boundary is equally good.
    """
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda item: (
            item.rank,
            item.score,
            -item.depth,
            item.block_chars,
        ),
    )


# --------------------------------------------------------------------------- #
# Oversized paragraphs
# --------------------------------------------------------------------------- #


def split_oversized_paragraph(
    line: str,
    maximum: int,
    *,
    quotation_depth: int = 0,
    bracket_depth: int = 0,
) -> Tuple[List[str], int]:
    """Split one source line longer than ``maximum`` into physical lines.

    Prefers ``。！？……`` then ``；`` then ``，：、``, searching backward from the
    limit, and never cuts inside an open quotation, an open ``《》``/``（）``
    expression, an ellipsis run, a number or a Latin token. Returns the segments
    and the number of forced splits (no safe boundary was available).
    """
    segments: List[str] = []
    forced = 0
    remaining = line
    quote = quotation_depth
    bracket = bracket_depth
    while len(remaining) > maximum:
        cut, was_forced = _find_split(remaining, maximum, quote, bracket)
        forced += 1 if was_forced else 0
        segment = remaining[:cut].rstrip()
        if not segment:  # never emit a whitespace-only line
            cut = maximum
            forced += 1
            segment = remaining[:cut].rstrip()
        if not segment:
            break
        segments.append(segment)
        quote = _advance_depth(quote, segment)
        bracket = _advance_bracket_depth(bracket, segment)
        remaining = remaining[cut:].lstrip()
    tail = remaining.strip()
    if tail:
        segments.append(tail)
    return segments, forced


def _find_split(
    text: str,
    maximum: int,
    quotation_depth: int,
    bracket_depth: int,
) -> Tuple[int, bool]:
    """Best cut position (<= maximum) and whether it had to be forced."""
    limit = min(maximum, len(text) - 1)
    if limit < 1:
        return len(text), True
    quotes = _prefix_depth(text, quotation_depth, QUOTATION_OPEN, QUOTATION_CLOSE)
    brackets = _prefix_bracket_depth(text, bracket_depth)
    for stops in (_STRONG_STOPS, _MEDIUM_STOPS, _WEAK_STOPS):
        best = 0
        for position in range(1, limit + 1):
            if text[position - 1] not in stops:
                continue
            cut = _absorb_trailing(text, position, limit)
            if cut <= limit and _safe_split(text, cut, quotes, brackets):
                best = max(best, cut)
        if best:
            return best, False
    for position in range(limit, 0, -1):
        if _safe_split(text, position, quotes, brackets):
            return position, False
    return limit, True


def _absorb_trailing(text: str, position: int, limit: int) -> int:
    """Keep an ellipsis run and closing marks attached to the left segment."""
    while position < limit and text[position] in "…" + _TRAILING_CLOSERS:
        position += 1
    return position


def _safe_split(
    text: str,
    position: int,
    quotes: List[int],
    brackets: List[int],
) -> bool:
    if position <= 0:
        return False
    if position >= len(text):
        return True
    if quotes[position] > 0:
        return False  # never cut inside an open quotation
    if brackets[position] > 0:
        return False  # never cut inside 《...》 / （...）
    before, after = text[position - 1], text[position]
    if before == "…" or after == "…":
        return False  # never split an ellipsis sequence
    if before.isdigit() and after.isdigit():
        return False  # never split a number
    if before.isdigit() and after in "年月日时分秒岁次":
        return False  # never split a date expression
    if _inside_latin_token(text, position):
        return False  # never split a Latin word/token
    return True


def _inside_latin_token(text: str, position: int) -> bool:
    left = position
    while left > 0 and _is_token_char(text[left - 1]):
        left -= 1
    right = position
    while right < len(text) and _is_token_char(text[right]):
        right += 1
    return left < position < right and (right - left) >= 2


def _is_token_char(char: str) -> bool:
    return char.isascii() and char.isalnum()


def _prefix_depth(text: str, initial: int, openers: str, closers: str) -> List[int]:
    depths = [initial]
    for char in text:
        depth = depths[-1]
        if char in openers:
            depth += 1
        elif char in closers and depth > 0:
            depth -= 1
        depths.append(depth)
    return depths


def _prefix_bracket_depth(text: str, initial: int) -> List[int]:
    depths = [initial]
    for char in text:
        depths.append(_step_bracket_depth(depths[-1], char))
    return depths


def _advance_bracket_depth(depth: int, text: str) -> int:
    for char in text:
        depth = _step_bracket_depth(depth, char)
    return depth


def _step_bracket_depth(depth: int, char: str) -> int:
    for opener, closer in _TITLE_BRACKETS:
        if char == opener:
            depth += 1
        elif char == closer and depth > 0:
            depth -= 1
    return depth


# --------------------------------------------------------------------------- #
# Chapter chunking
# --------------------------------------------------------------------------- #


def source_layout(text: str) -> Tuple[List[str], set]:
    """Source lines (verbatim, whitespace-only ones dropped) and blank breaks.

    ``sections`` holds the index of every line that the crawled text separated
    from the next line with a blank line: those are the strongest boundaries.
    """
    lines: List[str] = []
    sections = set()
    pending_blank = False
    for raw in normalize_newlines(text).split("\n"):
        if not raw.strip():
            if lines:
                pending_blank = True
            continue
        if pending_blank:
            sections.add(len(lines) - 1)
            pending_blank = False
        lines.append(raw)
    return lines, sections


def build_chapter_blocks(
    lines: List[str],
    *,
    target: int,
    maximum: int,
    sections: Optional[Iterable[int]] = None,
) -> ChapterBlocks:
    """Chunk one chapter's source lines into contiguous blocks.

    Blocks are contiguous and never overlap: every source line appears exactly
    once, in order, on its own physical line. Only blank-line block boundaries
    are added, and no block exceeds ``maximum`` characters.
    """
    result = ChapterBlocks()
    total_lines = len(lines)
    if not total_lines:
        return result

    prefix = [0]
    for line in lines:
        prefix.append(prefix[-1] + len(line))
    entire = prefix[-1]
    quotations = quotation_depths(lines)
    brackets = bracket_depths(lines)
    section_after = set(sections or ())

    index = 0
    index_base = 0
    expanded: List[str] = []
    while index < total_lines:
        start = index
        total = 0
        while index < total_lines:
            length = len(lines[index])
            if total and total + length > maximum:
                break  # keep the paragraph whole: it moves to the next block
            total += length
            index += 1
        end = index

        if end - start == 1 and total > maximum:
            if NATIVE_HEADING.match(lines[start]):
                # A native chapter heading is never split, even when a
                # pathological source line exceeds the limit. It is kept whole,
                # reported, and exempted from the block-size invariant.
                result.blocks.append([lines[start]])
                result.block_sizes.append(total)
                result.oversized_headings += 1
                result.oversized_heading_sizes.append(total)
                expanded.append(lines[start])
                index = end
                index_base = end
                continue
            segments, forced = split_oversized_paragraph(
                lines[start],
                maximum,
                quotation_depth=quotations[start],
                bracket_depth=brackets[start],
            )
            result.oversized_splits += 1
            result.oversized_segments += len(segments)
            result.forced_splits += forced
            # The heading (or any prior line) stays in its own block; the split
            # segments follow as their own blocks. Content stays contiguous.
            if start > index_base:
                head = lines[index_base:start]
                result.blocks.append(head)
                result.block_sizes.append(prefix[start] - prefix[index_base])
                expanded.extend(head)
            for segment in segments:
                result.blocks.append([segment])
                result.block_sizes.append(len(segment))
            expanded.extend(segments)
            index = end
            index_base = end
            continue

        boundary = _choose_boundary(
            lines,
            start,
            end,
            prefix=prefix,
            entire=entire,
            quotations=quotations,
            brackets=brackets,
            section_after=section_after,
            target=target,
            maximum=maximum,
        )
        cut = boundary.index if boundary else end
        block = lines[start:cut]
        if not block:  # defensive: always make progress
            cut = end
            block = lines[start:end]
            boundary = None
            index_base = cut
        result.blocks.append(block)
        result.block_sizes.append(prefix[cut] - prefix[start])
        expanded.extend(block)
        index_base = cut
        # A cut only becomes a block boundary when another block follows; a cut
        # at the chapter's end inserts no blank line and is not a boundary.
        if boundary is not None and cut < total_lines:
            result.boundaries.append(boundary)
            if boundary.kind in ("medium", "weak"):
                result.weak_fallbacks += 1
            if boundary.inside_quotation:
                result.open_quote_cuts += 1
            if 0 < boundary.next_chars < SHORT_TAIL_CHARS:
                result.stunted_tail_cuts += 1
        index = cut
    result.lines = expanded
    return result


def _choose_boundary(
    lines: List[str],
    start: int,
    end: int,
    *,
    prefix: List[int],
    entire: int,
    quotations: List[int],
    brackets: List[int],
    section_after: set,
    target: int,
    maximum: int,
) -> Optional[Boundary]:
    """Best boundary inside the filled span, or its end when none is in reach."""
    total_lines = len(lines)
    floor = max(0, target - SAFE_BLOCK_SLACK)
    candidates: List[Boundary] = []
    for cut in range(start + 1, end + 1):
        block_chars = prefix[cut] - prefix[start]
        if cut < end and block_chars < floor:
            continue  # too early: cutting here would waste the block
        previous = lines[cut - 1]
        following = lines[cut] if cut < total_lines else ""
        inside_quotation = quotations[cut] > 0
        closed_quotation = quotations[cut] == 0 and any(
            mark in previous for mark in QUOTATION_CLOSE
        )
        kind = classify_boundary(
            previous,
            following,
            section=(cut - 1) in section_after,
            closed_quotation=closed_quotation,
        )
        next_chars = entire - prefix[cut]
        candidates.append(
            Boundary(
                index=cut,
                kind=kind,
                score=score_boundary(
                    kind,
                    ending=_ending_class(previous),
                    block_chars=block_chars,
                    next_chars=next_chars,
                    speech_intro=bool(following) and _is_speech_intro(previous, following),
                    inside_quotation=inside_quotation,
                    inside_brackets=brackets[cut] > 0,
                    target=target,
                    maximum=maximum,
                ),
                block_chars=block_chars,
                next_chars=next_chars,
                inside_quotation=inside_quotation,
                depth=quotations[cut],
            )
        )
    chosen = select_boundary(candidates)
    if chosen is not None:
        return chosen
    if end > start:
        return Boundary(
            index=end,
            kind="paragraph",
            score=_BOUNDARY_SCORE["paragraph"],
            block_chars=prefix[end] - prefix[start],
            next_chars=entire - prefix[end],
            inside_quotation=quotations[end] > 0,
            depth=quotations[end],
        )
    return None



# --------------------------------------------------------------------------- #
# Export assembly
# --------------------------------------------------------------------------- #

NATIVE_HEADING = re.compile(r"^\s*第\s*(?:\d+|[零〇一二三四五六七八九十百千万两]+)\s*[章回节]")
_REPORT_ID_LIMIT = 50


@dataclass
class ExportText:
    """Assembled TXT plus the audit and the per-chapter source content."""

    text: str
    audit: Dict[str, Any] = field(default_factory=dict)
    chapters: List[Tuple[Any, List[str]]] = field(default_factory=list)
    front_matter: List[str] = field(default_factory=list)
    block_sizes: List[int] = field(default_factory=list)
    oversized_heading_sizes: List[int] = field(default_factory=list)


def _plain_text(html: str) -> str:
    """Flatten crawled HTML exactly as this binder always has."""
    from ..utils.html_tools import extract_text

    return extract_text(html)


def chapter_content(chapter: Any) -> str:
    """Plain text of a chapter body. HTML is flattened exactly as before."""
    return _plain_text(chapter.body or "")


def new_audit(target: int, maximum: int) -> Dict[str, Any]:
    """Audit record for one export. Never written into the TXT itself."""
    return {
        "chapters_exported": 0,
        "safe_block_target": target,
        "safe_block_max": maximum,
        "safe_blocks_created": 0,
        "blank_boundaries_inserted": 0,
        "oversized_paragraphs_split": 0,
        "oversized_headings_kept": 0,
        "maximum_block_chars": 0,
        "minimum_block_chars": 0,
        "average_block_chars": 0.0,
        "boundaries_at_paragraph": 0,
        "boundaries_at_sentence": 0,
        "boundaries_using_weak_fallback": 0,
        # diagnostics: explicitly reported exceptions, never silent
        "boundary_kinds": {},
        "forced_splits": 0,
        "stunted_tail_cuts": 0,
        "boundaries_inside_open_quotation": 0,
        "empty_chapter_bodies": 0,
        "chapters_without_native_heading": 0,
        "heading_missing_report_only": 0,
        "heading_missing_chapter_ids": [],
        "duplicate_native_heading_report_only": 0,
        "duplicate_heading_chapter_ids": [],
    }


def front_matter_lines(novel: Any, chapter_count: int) -> List[str]:
    """Metadata header, unchanged apart from collapsing blank runs."""
    synopsis = "\n".join(content_lines(_plain_text(novel.synopsis or "")))
    return [
        novel.title or "",
        f"by {novel.author}" if novel.author else "",
        "",
        CHAPTER_SEPARATOR,
        "",
        synopsis,
        "",
        f"Source: {novel.url}",
        f"Tags: {', '.join(novel.tags or [])}",
        f"Volumes: {len(novel.volumes)}",
        f"Chapters: {chapter_count}",
        "",
        FRONT_MATTER_MARKER,
    ]

def build_export_text(
    novel: Any,
    chapters: List[Any],
    *,
    target: Optional[int] = None,
    maximum: Optional[int] = None,
) -> ExportText:
    """Chunk every successful chapter and assemble the whole TXT."""
    resolved_target, resolved_maximum = resolve_safe_block_limits(target, maximum)
    included = [chapter for chapter in chapters if chapter.success]
    front_matter = front_matter_lines(novel, len(included))

    audit = new_audit(resolved_target, resolved_maximum)
    lines: List[str] = list(front_matter)
    records: List[Tuple[Any, List[str]]] = []
    block_sizes: List[int] = []
    oversized_heading_sizes: List[int] = []

    for chapter in included:
        source, sections = source_layout(chapter_content(chapter))
        built = build_chapter_blocks(
            source,
            target=resolved_target,
            maximum=resolved_maximum,
            sections=sections,
        )
        if "".join(built.content) != "".join(source):
            raise LNException(
                f"Chapter {chapter.id}: VietPhrase block layout changed the crawled content"
            )
        records.append((chapter, list(built.content)))
        block_sizes.extend(built.block_sizes)
        audit["chapters_exported"] += 1
        audit["safe_blocks_created"] += len(built.blocks)
        audit["blank_boundaries_inserted"] += max(0, len(built.blocks) - 1)
        audit["oversized_paragraphs_split"] += built.oversized_splits
        audit["oversized_headings_kept"] += built.oversized_headings
        oversized_heading_sizes.extend(built.oversized_heading_sizes)
        audit["forced_splits"] += built.forced_splits
        audit["stunted_tail_cuts"] += built.stunted_tail_cuts
        record_boundaries(audit, built)
        record_heading(audit, chapter, source)
        if not source:
            audit["empty_chapter_bodies"] += 1

        lines.append("")  # exactly one blank line before the chapter separator
        lines.append(CHAPTER_SEPARATOR)
        for position, block in enumerate(built.blocks):
            if position:
                lines.append("")
            lines.extend(block)

    while lines and not lines[-1].strip():
        lines.pop()
    text = "\n".join(lines) + "\n"

    audit["minimum_block_chars"] = min(block_sizes) if block_sizes else 0
    audit["maximum_block_chars"] = max(block_sizes) if block_sizes else 0
    audit["average_block_chars"] = (
        round(sum(block_sizes) / len(block_sizes), 1) if block_sizes else 0.0
    )
    return ExportText(
        text=text,
        audit=audit,
        chapters=records,
        front_matter=front_matter,
        block_sizes=block_sizes,
        oversized_heading_sizes=oversized_heading_sizes,
    )


def record_boundaries(audit: Dict[str, Any], built: ChapterBlocks) -> None:
    """Count chosen boundary classes and the exceptions they represent."""
    kinds = audit["boundary_kinds"]
    for boundary in built.boundaries:
        kinds[boundary.kind] = kinds.get(boundary.kind, 0) + 1
        if boundary.kind in ("section", "dialogue", "paragraph"):
            audit["boundaries_at_paragraph"] += 1
        elif boundary.kind in ("sentence", "medium"):
            audit["boundaries_at_sentence"] += 1
        else:
            audit["boundaries_using_weak_fallback"] += 1
        if boundary.inside_quotation:
            audit["boundaries_inside_open_quotation"] += 1
    extra = built.oversized_segments - 1
    if extra > 0:
        # Each extra segment of a split paragraph also became its own block, so
        # it is a sentence-level boundary for audit purposes.
        kinds["oversized_split"] = kinds.get("oversized_split", 0) + extra
        audit["boundaries_at_sentence"] += extra


def record_heading(audit: Dict[str, Any], chapter: Any, source: List[str]) -> None:
    """Report heading diagnostics.

    A chapter whose crawled body has no native heading is kept verbatim: it is
    reported, never rejected, and no heading is ever synthesized for it.
    """
    if not source:
        return
    if not NATIVE_HEADING.match(source[0]):
        audit["chapters_without_native_heading"] += 1
        audit["heading_missing_report_only"] += 1
        ids = audit["heading_missing_chapter_ids"]
        if len(ids) < _REPORT_ID_LIMIT:
            ids.append(chapter.id)
        return
    if _has_duplicate_heading(source):
        audit["duplicate_native_heading_report_only"] += 1
        ids = audit["duplicate_heading_chapter_ids"]
        if len(ids) < _REPORT_ID_LIMIT:
            ids.append(chapter.id)


def _has_duplicate_heading(source: List[str]) -> bool:
    seen: Dict[str, int] = {}
    for line in source:
        if not NATIVE_HEADING.match(line):
            continue
        key = "".join(char for char in line if not char.isspace())
        seen[key] = seen.get(key, 0) + 1
        if seen[key] > 1:
            return True
    return False

# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


def validate_export(built: ExportText) -> None:
    """Enforce the fatal export invariants; report-only diagnostics never fail."""
    audit = built.audit
    maximum = int(audit.get("safe_block_max") or SAFE_BLOCK_MAX)
    front = "\n".join(built.front_matter)
    if WRAPPER_PATTERN.search(front):
        raise LNException("Export validation failed: synthetic wrapper in front matter")
    marker = FRONT_MATTER_MARKER
    pos = built.text.find(marker)
    if pos < 0:
        raise LNException("Export validation failed: front-matter marker missing")
    if "\r" in built.text:
        raise LNException("Export validation failed: non-LF line ending")
    try:
        built.text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise LNException(f"Export validation failed: not UTF-8: {exc}") from exc
    after = normalize_newlines(built.text[pos + len(marker):])
    tail = after.strip("\n")
    if not tail and audit.get("chapters_exported"):
        raise LNException("Export validation failed: chapter region is empty")
    raw_parts = tail.split(CHAPTER_SEPARATOR) if tail else []
    # Each chapter is "\n" + separator + "\n" + body; the text before the
    # first separator is only leftover front-matter spacing.
    parts = []
    for position, raw in enumerate(raw_parts):
        if position == 0:
            if raw.strip("\n"):
                raise LNException(
                    "Export validation failed: text before first chapter separator"
                )
            continue
        if not raw.startswith("\n"):
            raise LNException(
                "Export validation failed: chapter separator without newline"
            )
        parts.append(raw[1:])
    if len(parts) != audit.get("chapters_exported", 0):
        raise LNException(
            "Export validation failed: chapter separator count "
            f"({len(parts)}) != chapters exported "
            f"({audit.get('chapters_exported', 0)})"
        )
    written = []
    for part in parts:
        # block blanks are single empty lines; dropping them must recover source
        written.extend(line for line in part.split("\n") if line != "")
    expected = []
    for _, source in built.chapters:
        expected.extend(source)
    if written != expected:
        raise LNException(
            "Export validation failed: content mismatch "
            "(order/deletion/duplication/merge)"
        )
    for _, source in built.chapters:
        for line in source:
            if WRAPPER_PATTERN.match(line):
                raise LNException(
                    f"Export validation failed: synthetic wrapper remains: {line!r}"
                )
    over_maximum = [size for size in built.block_sizes if size > maximum]
    reported = sorted(built.oversized_heading_sizes)
    if sorted(over_maximum) != reported:
        raise LNException(
            f"Export validation failed: block over {maximum} chars "
            f"({len(over_maximum)} block(s), {len(reported)} reported unsplittable heading(s))"
        )
    if len(reported) != audit.get("oversized_headings_kept", 0):
        raise LNException(
            "Export validation failed: oversized native headings are not reported in the audit"
        )
    for line in built.text.split("\n"):
        if line != "" and not line.strip():
            raise LNException("Export validation failed: whitespace-only line")
