"""Deterministic chapter candidates, document structure, and safe-boundary chunking.

A heading regex is deliberately only a lexer. TOC evidence, volume transitions,
body content, and duplicate-title evidence decide which candidates are boundaries.
Original inputs remain in the job store. Confirmed exporter/body duplicates are
omitted from prose and retained verbatim in parser diagnostics.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass

from .models import Chapter

logger = logging.getLogger(__name__)
NUMERAL = r"[零〇一二两三四五六七八九十百千万\d]+"
HEADING = re.compile(
    rf"^\s*(?:第\s*({NUMERAL})\s*[章回节]|(?:chapter|chương)\s+(\d+)\b)(.*)$",
    re.I,
)
VOLUME = re.compile(
    rf"^\s*(?:第\s*({NUMERAL})\s*卷|卷\s*({NUMERAL})|"
    rf"(?:volume|quyển)\s+({NUMERAL})\b)(.*)$",
    re.I,
)
HAN = re.compile(r"[\u3400-\u9fff]")
TOC_LABEL = re.compile(r"^(?:目\s*[录錄]|contents|table\s+of\s+contents|mục\s*lục)\s*[:：]?$", re.I)
DUPLICATE_MAX_CHARS = 80
DUPLICATE_MAX_PARAGRAPHS = 1


def chapter_number(value):
    if value.isdigit():
        return int(value)
    digits = {c: n for n, c in enumerate("零一二三四五六七八九")}
    digits.update({"〇": 0, "两": 2})
    if all(c in digits for c in value):
        return int("".join(str(digits[c]) for c in value))
    total = section = number = 0
    for c in value:
        if c in digits or c.isdigit():
            number = digits[c] if c in digits else int(c)
        elif c == "万":
            total += (section + number) * 10000
            section = number = 0
        else:
            section += (number or 1) * {"十": 10, "百": 100, "千": 1000}[c]
            number = 0
    return total + section + number


def has_meaningful_body_content(text):
    """Short dialogue counts; whitespace and purely decorative punctuation do not.

    Structural headings are excluded by the classifier, not by blindly testing
    another regex here: a demoted candidate may really be novel prose.
    """
    return any(char.isalnum() for char in text)


@dataclass
class Candidate:
    line: int
    text: str
    number: int
    title: str
    family: str
    kind: str = "chapter"

    @property
    def nested(self):
        return HEADING.match(self.title.lstrip(" :：-—"))


@dataclass
class Document:
    lines: list
    candidates: dict
    diagnostics: list
    skipped: set


class ChapterValidationError(ValueError):
    def __init__(self, message, **detail):
        super().__init__(message)
        self.detail = {
            "error": "chapter_validation_failed",
            "severity": "FATAL",
            "input": None,
            "line": None,
            "heading": None,
            "chapter": None,
            "previous_chapter": None,
            "previous_line": None,
            "reason": "invalid_structure",
            "message": message,
            **detail,
        }


def _fail(label, reason, message, candidate=None, previous=None, **extra):
    detail = {
        "input": label,
        "reason": reason,
        "line": candidate.line if candidate else None,
        "heading": candidate.text if candidate else None,
        "chapter": candidate.number if candidate else None,
        "previous_chapter": previous.number if previous else None,
        "previous_line": previous.source_line if previous else None,
        **extra,
    }
    raise ChapterValidationError(message, **detail)


def _record(document, label, severity, reason, candidate=None, previous=None, **extra):
    record = {
        "severity": severity,
        "input": label,
        "reason": reason,
        "line": candidate.line if candidate else None,
        "heading": candidate.text if candidate else None,
        "chapter": candidate.number if candidate else None,
        "previous_chapter": previous.number if previous else None,
        "previous_line": previous.source_line if previous else None,
        **extra,
    }
    document.diagnostics.append(record)
    logger.debug("Chapter parser: %s", record)


def _scan(text, label):
    # Remove only the leading BOM, retaining exact source line numbering.
    lines = text.lstrip("\ufeff").splitlines()
    candidates = {}
    for line_number, original in enumerate(lines, 1):
        line = original.strip()
        if "\ufffd" in line or "\x00" in line:
            _fail(
                label,
                "invalid_encoding",
                "Input contains invalid text; provide valid UTF-8 files",
                line=line_number,
            )
        match = HEADING.match(line)
        volume = VOLUME.match(line)
        if match:
            candidates[line_number] = Candidate(
                line_number,
                line,
                chapter_number(match[1] or match[2]),
                match[3],
                "han"
                if match[1]
                else ("vietnamese" if line.lower().startswith("chương") else "english"),
            )
        elif volume:
            candidates[line_number] = Candidate(
                line_number,
                line,
                chapter_number(volume[1] or volume[2] or volume[3]),
                volume[4],
                "volume",
                "volume",
            )
    return Document(lines, candidates, [], set())


def _detect_toc(document, label):
    """Only an introductory prose-free run corroborated by later bodies is skipped.

    The first body-bearing heading closes that run (it is not a TOC entry).
    Explicit exporter metadata before the run is handled separately from prose.
    """
    candidates = list(document.candidates.values())
    chapters = [c for c in candidates if c.kind == "chapter"]
    if len(chapters) < 3:
        return
    first = chapters[0].line
    prefix = document.lines[: first - 1]
    labeled = any(TOC_LABEL.fullmatch(line.strip()) for line in prefix)
    metadata = any(line.strip().startswith("Source:") for line in prefix)
    if (
        not labeled
        and not metadata
        and any(has_meaningful_body_content(line) and not VOLUME.match(line) for line in prefix)
    ):
        return
    # Prefix sums make both TOC lookahead and long exporter files linear-time.
    prose = [0]
    for index, text in enumerate(document.lines, 1):
        prose.append(
            prose[-1] + int(index not in document.candidates and has_meaningful_body_content(text))
        )

    def body_after(index):
        candidate = chapters[index]
        end = chapters[index + 1].line if index + 1 < len(chapters) else len(document.lines) + 1
        return prose[end - 1] - prose[candidate.line] > 0

    run = []
    for index, candidate in enumerate(chapters):
        if body_after(index):
            break
        run.append(candidate)
    # A real exporter boundary immediately before its embedded body heading
    # belongs to the novel, not to the dense TOC prefix.
    if run and len(run) < len(chapters):
        following = chapters[len(run)]
        while (
            run
            and run[-1].number == following.number
            and _duplicate_evidence(following, run[-1], [])
        ):
            following = run.pop()
    minimum = 2 if labeled else 3
    if len(run) < minimum or len(run) == len(chapters):
        return
    # Check ordered corroboration, not merely membership. Later real chapter
    # entries must have bodies and at least two must agree with the TOC run.
    positions = {}
    for index, candidate in enumerate(run):
        positions.setdefault(candidate.number, []).append(index)
    previous_position = -1
    matches = 0
    for index in range(len(run), len(chapters)):
        if not body_after(index):
            continue
        found = next(
            (p for p in positions.get(chapters[index].number, []) if p > previous_position), None
        )
        if found is not None:
            previous_position = found
            matches += 1
    if matches < 2:
        return
    end = run[-1].line
    for candidate in run:
        document.skipped.add(candidate.line)
        _record(document, label, "INFO", "toc_heading_skipped", candidate)
    for index in range(1, end + 1):
        if TOC_LABEL.fullmatch(document.lines[index - 1].strip()):
            document.skipped.add(index)
        candidate = document.candidates.get(index)
        if candidate and candidate.kind == "volume":
            document.skipped.add(index)


def _normalized_title(candidate):
    title = candidate.title.lstrip(" :：-—")
    nested = candidate.nested
    if nested:
        title = nested[3]
    return "".join(c.casefold() for c in title if c.isalnum())


def _is_reference(candidate):
    """Conservative, explicit prose syntax; never demote a backward header merely
    because its number is inconvenient. Titles remain allowed to be sentences.
    """
    if candidate.family == "han":
        return bool(
            re.match(
                r"^(?:(?:中|里|里面)(?:记载|提到|描述|说过|写道)|所述|提到|说过)", candidate.title
            )
        )
    return bool(
        re.match(r"^\s+(?:was|is|had|has|mentions|describes|đã|nói rằng)\b", candidate.title, re.I)
    )


def _duplicate_evidence(candidate, previous_candidate, meaningful):
    """Only a title-compatible heading with no meaningful content in between.

    Separators and blanks are not prose. Even one short meaningful fragment
    makes repeated same-volume numbering fatal.
    """
    if meaningful:
        return False
    left, right = _normalized_title(previous_candidate), _normalized_title(candidate)
    return not left or not right or left == right


# The RAW exporter writes "Chapter N: <nested chinese heading>" as the boundary
# line, then a decorative separator, then the same nested heading inside the
# downloaded chapter body. One chapter, not two.
EXPORTER_WRAPPER = re.compile(
    rf"^\s*chapter\s+(\d+)\s*[:：]\s*(第\s*({NUMERAL})\s*[章回节].*)$",
    re.I,
)
VIETPHRASE_EXPORTER_WRAPPER = re.compile(
    r"^\s*chapter\s+(\d+)\s*[:：]\s*((?:thứ\s*)?(\d+)\s*chương\b.*)$", re.I
)
VIETPHRASE_EMBEDDED_TITLE = re.compile(r"^\s*(?:thứ\s*)?(\d+)\s*chương\b.*$", re.I)


def _exporter_duplicate(
    document, candidate, current, current_candidate, meaningful, pending_volume_lines
):
    """Return diagnostic extra fields when this is exactly the exporter pattern.

    The wrapper's nested Chinese heading must match the embedded heading by
    normalized title; the full wrapper string is never compared. Only blanks
    and decorative separators may intervene.
    """
    if meaningful or pending_volume_lines or candidate.family != "han":
        return None
    wrapper = EXPORTER_WRAPPER.match(current_candidate.text)
    if not wrapper or int(wrapper[1]) != current_candidate.number:
        return None
    nested_number = chapter_number(wrapper[3])
    if nested_number != current_candidate.number or nested_number != candidate.number:
        return None
    # Normalize titles only after independently checking all three numbers.
    nested = HEADING.match(wrapper[2])

    def normalize(text):
        text = unicodedata.normalize("NFKC", text).casefold()
        return "".join(c for c in text if not c.isspace())

    left, right = normalize(nested[3]), normalize(candidate.title)
    match_kind = "normalized_exact"
    if "".join(c for c in left if c.isalnum()) != "".join(c for c in right if c.isalnum()):
        # Real exporter artifact: title cut off INSIDE a parenthetical note.
        # Not arbitrary fuzzy/prefix matching: the entire main title must match,
        # and the incomplete note must be an exact prefix of the complete note.
        main, opening, note = left.partition("(")
        full_main, full_opening, full_note = right.partition("(")
        if not (
            opening
            and full_opening
            and main
            and main == full_main
            and note
            and "(" not in note
            and ")" not in note
            and full_note.endswith(")")
            and "(" not in full_note
            and full_note.count(")") == 1
            and full_note.startswith(note)
            and len(full_note) > len(note)
        ):
            return None
        match_kind = "truncated_parenthetical"
    # Only blank/decorative source lines may occur between this exact pair;
    # even another suppressed structural heading is not decorative text.
    if any(
        has_meaningful_body_content(line)
        for line in document.lines[current_candidate.line : candidate.line - 1]
    ):
        return None
    return {
        "nested_heading": wrapper[2],
        "wrapper_line": current_candidate.line,
        "original_text": document.lines[candidate.line - 1],
        "volume": current.volume,
        "match_kind": match_kind,
        "action": "excluded_from_translation_prose",
        "message": "Exporter embedded heading excluded from prose; original retained in diagnostics",
    }


def _vietphrase_exporter_duplicate(
    document, line, line_number, current, current_candidate, meaningful, pending_volume_lines
):
    """Recognize the VietPhrase text binder's repeated title line.

    Its wrapper is ``Chapter N: Thứ N chương <title>`` but the embedded title
    begins with ``Thứ N chương`` and therefore is intentionally not a chapter
    boundary. It must still be removed before semantic alignment: unlike RAW,
    leaving it in the body creates a source paragraph with no counterpart.
    """
    if meaningful or pending_volume_lines:
        return None
    wrapper = VIETPHRASE_EXPORTER_WRAPPER.match(current_candidate.text)
    embedded = VIETPHRASE_EMBEDDED_TITLE.match(line)
    if not wrapper or not embedded:
        return None
    if int(wrapper[1]) != current.number or int(wrapper[3]) != current.number:
        return None
    if int(embedded[1]) != current.number:
        return None

    def normalize(text):
        return "".join(c for c in unicodedata.normalize("NFKC", text).casefold() if not c.isspace())

    left, right = normalize(wrapper[2]), normalize(line)
    match_kind = "normalized_exact"
    if "".join(c for c in left if c.isalnum()) != "".join(c for c in right if c.isalnum()):
        main, opening, note = left.partition("(")
        full_main, full_opening, full_note = right.partition("(")
        if not (
            opening
            and full_opening
            and main == full_main
            and main
            and note
            and "(" not in note
            and ")" not in note
            and full_note.endswith(")")
            and "(" not in full_note
            and full_note.count(")") == 1
            and full_note.startswith(note)
            and len(full_note) > len(note)
        ):
            return None
        match_kind = "truncated_parenthetical"
    if any(
        has_meaningful_body_content(source)
        for source in document.lines[current_candidate.line : line_number - 1]
    ):
        return None
    return {
        "wrapper_line": current_candidate.line,
        "original_text": document.lines[line_number - 1],
        "volume": current.volume,
        "match_kind": match_kind,
        "action": "excluded_from_translation_prose",
        "message": "Exporter embedded VietPhrase title excluded from prose; original retained in diagnostics",
    }


def parse_document(text, label):
    document = _scan(text, label)
    _detect_toc(document, label)
    chapters = []
    current = None
    current_candidate = None
    meaningful = []
    preamble = []
    pending_volume_lines = []
    volume = None
    # A standalone volume marker needs a following chapter before any prose.
    next_significant = {}
    following = None
    for index in range(len(document.lines), 0, -1):
        next_significant[index] = following
        if index not in document.skipped and has_meaningful_body_content(document.lines[index - 1]):
            following = index

    def finish():
        if current is not None:
            if not meaningful:
                _fail(
                    label,
                    "empty_chapter_body",
                    "Chapter has no meaningful body content",
                    current_candidate,
                    chapters[-2] if len(chapters) > 1 else None,
                    volume=current.volume,
                )
            current.meaningful_text = "\n".join(meaningful)

    for index, original in enumerate(document.lines, 1):
        if index in document.skipped:
            continue
        line = original.strip()
        if not line:
            continue
        candidate = document.candidates.get(index)
        if candidate and candidate.kind == "volume":
            next_candidate = document.candidates.get(next_significant[index])
            if next_candidate and next_candidate.kind == "chapter":
                if volume is not None and candidate.number < volume:
                    _fail(
                        label,
                        "volume_order_invalid",
                        f"Volume numbering moved backward from {volume} to {candidate.number}",
                        candidate,
                        current,
                        volume=candidate.number,
                        previous_volume=volume,
                    )
                reason = (
                    "repeated_volume_label"
                    if candidate.number == volume
                    else "volume_boundary_detected"
                )
                _record(
                    document, label, "INFO", reason, candidate, current, volume=candidate.number
                )
                volume = candidate.number
                pending_volume_lines.append(line)
                continue
            # An in-body reference/unsupported volume marker cannot reset order.
            candidate = None
        if candidate and candidate.kind == "chapter" and current and _is_reference(candidate):
            _record(
                document, label, "INFO", "heading_candidate_is_body_reference", candidate, current
            )
            candidate = None
        if candidate and candidate.kind == "chapter":
            if current and candidate.number == current.number and volume == current.volume:
                exporter = _exporter_duplicate(
                    document,
                    candidate,
                    current,
                    current_candidate,
                    meaningful,
                    pending_volume_lines,
                )
                is_exporter_pair = (
                    candidate.family == "han"
                    and EXPORTER_WRAPPER.match(current_candidate.text) is not None
                )
                if exporter is not None or (
                    not is_exporter_pair
                    and _duplicate_evidence(candidate, current_candidate, meaningful)
                ):
                    if exporter is None:
                        # Generic structural duplicate: keep the text as body content.
                        current.paragraphs.extend(pending_volume_lines)
                        pending_volume_lines = []
                        current.paragraphs.append(line)
                    else:
                        # Confirmed exporter wrapper/embedded pair: drop the
                        # wrapper's separator and the embedded duplicate from prose.
                        while current.paragraphs and not has_meaningful_body_content(
                            current.paragraphs[-1]
                        ):
                            current.paragraphs.pop()
                    _record(
                        document,
                        label,
                        "WARNING",
                        "duplicate_embedded_heading",
                        candidate,
                        current,
                        meaningful_characters=0,
                        **(
                            exporter
                            or {
                                "message": "Duplicate embedded heading preserved as body text, not a new boundary"
                            }
                        ),
                    )
                    continue
                _fail(
                    label,
                    "duplicate_chapter",
                    f"Chapter {candidate.number} repeats without a distinct volume; meaningful body or conflicting titles make this ambiguous",
                    candidate,
                    current,
                    volume=volume,
                )
            if current and candidate.number < current.number and volume == current.volume:
                _fail(
                    label,
                    "backward_numbering_without_volume_boundary",
                    f"Chapter numbering moved backward from {current.number} to {candidate.number} without a recognized volume boundary",
                    candidate,
                    current,
                    volume=volume,
                )
            finish()
            if current and volume != current.volume and candidate.number <= current.number:
                _record(
                    document,
                    label,
                    "INFO",
                    "volume_numbering_restart",
                    candidate,
                    current,
                    volume=volume,
                )
            previous = current
            current = Chapter(
                number=candidate.number,
                title=line,
                paragraphs=list(pending_volume_lines),
                volume=volume,
                source_line=index,
                input_label=label,
                previous_number=previous.number if previous else None,
                previous_line=previous.source_line if previous else None,
            )
            pending_volume_lines = []
            current_candidate = candidate
            meaningful = []
            chapters.append(current)
            _record(
                document, label, "INFO", "accepted_boundary", candidate, previous, volume=volume
            )
            continue
        if current:
            vietphrase_duplicate = _vietphrase_exporter_duplicate(
                document, line, index, current, current_candidate, meaningful, pending_volume_lines
            )
            if vietphrase_duplicate is not None:
                while current.paragraphs and not has_meaningful_body_content(
                    current.paragraphs[-1]
                ):
                    current.paragraphs.pop()
                duplicate = Candidate(index, line, current.number, line, "vietnamese")
                _record(
                    document,
                    label,
                    "WARNING",
                    "duplicate_embedded_vietphrase_title",
                    duplicate,
                    current,
                    meaningful_characters=0,
                    **vietphrase_duplicate,
                )
                continue
            current.paragraphs.append(line)
            if has_meaningful_body_content(line):
                meaningful.append(line)
        elif has_meaningful_body_content(line):
            preamble.append((index, line))
    if not chapters:
        _fail(
            label,
            "no_chapter_headings",
            "Chapter headings required: 第1章, Chapter 1, or Chương 1",
            line=1,
        )
    finish()
    if pending_volume_lines:
        current.paragraphs.extend(pending_volume_lines)
    # Metadata is only ignored under the pre-existing export contract. TOC
    # candidates are skipped separately; neighboring prose is not discarded.
    if preamble and not any(line.startswith("Source:") for _, line in preamble):
        _fail(
            label,
            "unheaded_content",
            "Unheaded content before first chapter; remove metadata or add a heading",
            line=preamble[0][0],
            heading=preamble[0][1],
        )
    counts = {}
    for diagnostic in document.diagnostics:
        counts[diagnostic["reason"]] = counts.get(diagnostic["reason"], 0) + 1
    logger.info("%s chapter parser: %s", label, counts)
    return document, chapters


def parse_chapters(text, label="RAW", diagnostics=None):
    document, chapters = parse_document(text, label)
    if diagnostics is not None:
        diagnostics.extend(document.diagnostics)
    return chapters


def _chapter_detail(chapter):
    return {
        "input": chapter.input_label,
        "line": chapter.source_line,
        "heading": chapter.title,
        "chapter": chapter.number,
        "volume": chapter.volume,
    }


def _alignment_fail(reason, message, actual, expected=None, previous=None):
    candidate = (
        Candidate(actual.source_line, actual.title, actual.number, "", "") if actual else None
    )
    _fail(
        actual.input_label if actual else "VIETPHRASE",
        reason,
        message,
        candidate,
        previous,
        volume=actual.volume if actual else None,
        counterpart=_chapter_detail(expected) if expected else None,
    )


def pair_chapters(raw, vp):
    """Exact ordered pairing after independent parsing; no skips, sort, or title match."""
    # One-sided volume metadata is safe only for globally unique matching number
    # sequences. Repeated-number sequences require explicit structure on both sides.
    raw_numbers, vp_numbers = [c.number for c in raw], [c.number for c in vp]
    one_sided = bool(any(c.volume is not None for c in raw)) != bool(
        any(c.volume is not None for c in vp)
    )
    transfer = one_sided and raw_numbers == vp_numbers and len(set(raw_numbers)) == len(raw_numbers)
    for index, (r, v) in enumerate(zip(raw, vp)):
        if r.number != v.number or (r.volume != v.volume and not transfer):
            _alignment_fail(
                "chapter_identity_mismatch",
                f"RAW and VIETPHRASE diverge at chapter position {index + 1}: RAW {r.key}, VIETPHRASE {v.key}",
                v,
                r,
                vp[index - 1] if index else None,
            )
    if len(raw) != len(vp):
        index = min(len(raw), len(vp))
        actual = vp[index] if index < len(vp) else None
        expected = raw[index] if index < len(raw) else None
        _alignment_fail(
            "chapter_count_mismatch",
            f"RAW has {len(raw)} chapters; VIETPHRASE has {len(vp)}. Missing or extra chapter at position {index + 1}",
            actual,
            expected,
            vp[index - 1] if index else None,
        )
    if transfer:
        for r, v in zip(raw, vp):
            r.volume = v.volume = r.volume if r.volume is not None else v.volume
    for r in raw:
        source = r.meaningful_text if r.meaningful_text is not None else "\n".join(r.paragraphs)
        if not HAN.search(source):
            candidate = Candidate(r.source_line, r.title, r.number, "", "")
            _fail(
                "RAW",
                "missing_chinese_source",
                "RAW chapter must contain Chinese source prose",
                candidate,
                volume=r.volume,
                previous_chapter=r.previous_number,
                previous_line=r.previous_line,
            )
    return list(zip(raw, vp))


def validate_inputs(raw_text, vp_text):
    raw = parse_chapters(raw_text, "RAW")
    vp = parse_chapters(vp_text, "VIETPHRASE")
    return pair_chapters(raw, vp)


def validate_alignment(alignment, raw, vp):
    if not alignment.confirmed:
        raise ValueError(f"Semantic alignment unconfirmed for chapter {raw.number}")
    for side, chapter in (("raw", raw), ("vp", vp)):
        flattened = [i for group in alignment.groups for i in getattr(group, side)]
        if flattened != list(range(len(chapter.paragraphs))):
            expected = set(range(len(chapter.paragraphs)))
            seen = set(flattened)
            raise ValueError(
                f"Alignment must cover every {side} paragraph once, monotonically; "
                f"expected IDs 0–{len(chapter.paragraphs) - 1}, "
                f"missing={sorted(expected - seen)[:20]}, "
                f"unexpected={sorted(seen - expected)[:20]}, "
                f"duplicates={sorted({i for i in flattened if flattened.count(i) > 1})[:20]}, "
                f"returned={len(flattened)}"
            )


def make_chunks(alignment, raw, vp):
    size = sum(map(len, raw.paragraphs))
    target = (
        size
        if size < 7000
        else (size / 2 if size <= 12000 else size / 3 if size <= 18000 else 5000)
    )
    chunks, groups, length = [], [], 0
    for group in alignment.groups:
        groups.append(group)
        length += sum(len(raw.paragraphs[i]) for i in group.raw)
        if length >= target and group.safe_break:
            chunks.append(groups)
            groups, length = [], 0
    if groups:
        chunks.append(groups)
    result = []
    for groups in chunks:
        result.append(
            {
                "raw": [{"id": i, "text": raw.paragraphs[i]} for g in groups for i in g.raw],
                "vp": [{"id": i, "text": vp.paragraphs[i]} for g in groups for i in g.vp],
            }
        )
    return result
