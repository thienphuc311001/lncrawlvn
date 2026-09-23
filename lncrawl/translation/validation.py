"""Deterministic structural, terminology, numeric, and truncation validation."""

from __future__ import annotations

import os
import re
import unicodedata
from collections import Counter
from decimal import Decimal

from .dictionary import locked_matches
from .models import Translation

HAN = re.compile(r"[\u3400-\u9fff]")
ARABIC_NUMBER = re.compile(r"(?<![0-9A-Za-z])\d+(?:[.,]\d+)*(?![0-9A-Za-z])")
CHINESE_CONTEXT_NUMBER = re.compile(
    r"(?<![0-9.,])([零〇一二两三四五六七八九十百千万亿]+)(?="
    r"个|人|名|位|年|月|日|天|时|点|分|秒|章|回|卷|页|号|次|遍|斤|两|银|钱|克|米|里|倍|成|层|岁|件|本|册|枚|颗|条|匹|辆|艘|万|亿)"
)
WRAPPERS = re.compile(
    r"^\s*(?:```|#\s|<(?:translation|chapter)>|\{\s*\"(?:title|segments)\")", re.I
)
TITLE_WRAPPER = re.compile(r"^\s*(?:Chương|Chapter)\s*\d+\s*[:：.\-]?\s*", re.I)
TERMINAL_SOURCE = re.compile(r"[。！？!?…」』”）】]$")
TERMINAL_VI = re.compile(r"[.!?…:;”’\"')\]]$")
FUNCTION_ENDING = re.compile(
    r"(?:\b(?:và|hoặc|nhưng|mà|vì|bởi|để|với|của|rằng|thì|là|đã|đang|sẽ|"
    r"không|chẳng|chưa|mới|vừa|bị|được|từ|đến|trong|ngoài|trên|dưới|như|nếu|khi|"
    r"bắt đầu|không phải)\s*)$",
    re.I,
)
CLAUSES = re.compile(r"[，,；;：:。.!！？?]")
SEMANTIC_SUSPICIONS = {"suspicious_length_ratio", "suspicious_truncation",
                       "suspicious_numeric_mismatch"}
SOURCE_CENSORSHIP = re.compile(r"(?<=[\u3400-\u9fff])\*+(?=[\u3400-\u9fff])|\*{2,}")
VI_SCALE = re.compile(r"^\s*(nghìn|ngàn|triệu|tỷ|tỉ|vạn)\b", re.I)
VI_SCALE_VALUES = {"nghìn": 1_000, "ngàn": 1_000, "triệu": 1_000_000,
                   "tỷ": 1_000_000_000, "tỉ": 1_000_000_000, "vạn": 10_000}
ZH_SCALE_VALUES = {"万": 10_000, "亿": 100_000_000}
VI_WORD_NUMBERS = {"một": "1", "hai": "2", "ba": "3", "bốn": "4", "năm": "5",
                   "sáu": "6", "bảy": "7", "tám": "8", "chín": "9", "mười": "10"}
VI_NUMBER_WORD_CONTEXT = re.compile(
    r"\b(?:bậc|thứ|căn|mũ|số)\s+(một|hai|ba|bốn|năm|sáu|bảy|tám|chín|mười)\b", re.I
)


def _normalize_number(value, source=False, scaled=False):
    """Return a decimal value, distinguishing common grouping from fractions."""
    separators = re.findall(r"[.,]", value)
    if len(separators) > 1:
        groups = re.split(r"[.,]", value)
        if len(set(separators)) == 1 and all(len(group) == 3 for group in groups[1:]):
            value = "".join(groups)
        else:
            value = "".join(groups[:-1]) + "." + groups[-1]
    elif separators:
        separator = separators[0]
        before, after = value.split(separator)
        grouping = (len(after) == 3 and not scaled
                    and ((source and separator == ",") or (not source and separator == ".")))
        value = before + after if grouping else before + "." + after
    return Decimal(value)


def _number_key(value):
    if not value:
        return "0"
    rendered = format(value.normalize(), "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def _chinese_integer(value):
    digits = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    units = {"十": 10, "百": 100, "千": 1000, "万": 10_000, "亿": 100_000_000}
    if re.search(r"[百千万亿][一二三四五六七八九]$", value) and "零" not in value:
        # Colloquial forms such as 一万二 and 一百二 are context dependent.
        return None
    if all(char in digits for char in value):
        return int("".join(str(digits[char]) for char in value))
    total = section = number = 0
    for char in value:
        if char in digits:
            number = digits[char]
            continue
        unit = units.get(char)
        if unit is None:
            return None
        if unit < 10_000:
            section += (number or 1) * unit
        else:
            section = (section + number) * unit
            total += section
            section = 0
        number = 0
    return total + section + number


def numeric_values(text, source=False, source_literals=()):
    values = Counter()
    for match in ARABIC_NUMBER.finditer(text):
        tail = text[match.end():]
        if source:
            scale = ZH_SCALE_VALUES.get(tail[:1], 1)
        else:
            unit = VI_SCALE.match(tail)
            scale = VI_SCALE_VALUES[unit[1].lower()] if unit else 1
        same_literal = not source and scale == 1 and match[0] in source_literals
        value = _normalize_number(match[0], source=source or same_literal, scaled=scale != 1)
        values[_number_key(value * scale)] += 1
    return values


def vietnamese_numeric_allowances(text):
    return Counter(VI_WORD_NUMBERS[match[1].lower()]
                   for match in VI_NUMBER_WORD_CONTEXT.finditer(text))


def chinese_numeric_allowances(text):
    values = Counter()
    ambiguous = False
    for match in CHINESE_CONTEXT_NUMBER.finditer(text):
        value = _chinese_integer(match[1])
        if value is not None:
            values[str(value)] += 1
        else:
            ambiguous = True
    return values, ambiguous


def numeric_mismatch(raw, translated):
    """Return missing/extra values while allowing digits rendered from Chinese numerals."""
    source = numeric_values(raw, source=True)
    source_literals = {
        match[0] for match in ARABIC_NUMBER.finditer(raw)
        if raw[match.end():match.end() + 1] not in ZH_SCALE_VALUES
    }
    target = numeric_values(translated, source_literals=source_literals)
    missing = source - target
    missing -= vietnamese_numeric_allowances(translated)
    extras = target - source
    allowances, ambiguous = chinese_numeric_allowances(raw)
    extras -= allowances
    if missing or extras:
        return {"missing": dict(missing), "extra": dict(extras), "ambiguous": ambiguous}
    return None


def _locked_target_present(target, text):
    """Ignore capitalization without ignoring spelling or Vietnamese marks."""
    target = unicodedata.normalize("NFC", target).casefold()
    text = unicodedata.normalize("NFC", text).casefold()
    return target in text


def _balanced(text):
    for left, right in (("“", "”"), ("‘", "’"), ("「", "」"), ("『", "』"),
                        ("(", ")"), ("[", "]"), ("（", "）"), ("【", "】"), ("«", "»")):
        if text.count(left) != text.count(right):
            return False
    if text.count('"') % 2:
        return False
    return True


def structural_findings(expected_ids, segments):
    """Validate the response envelope without an AI call."""
    actual = [segment.id for segment in segments]
    expected = list(expected_ids)
    findings = []
    duplicates = sorted(value for value, count in Counter(actual).items() if count > 1)
    if duplicates:
        findings.append({"kind": "duplicate_id", "ids": duplicates})
    missing = [value for value in expected if value not in actual]
    unexpected = [value for value in actual if value not in expected]
    if missing:
        findings.append({"kind": "missing_id", "ids": missing})
    if unexpected:
        findings.append({"kind": "unexpected_id", "ids": unexpected})
    if not missing and not unexpected and actual != expected:
        findings.append({"kind": "out_of_order_id", "expected": expected, "actual": actual})
    for segment in segments:
        if not segment.text.strip():
            findings.append({"kind": "empty_segment", "id": segment.id})
    return findings


def _truncation_signals(raw, vi):
    if not raw or not vi:
        return []
    signals = []
    ratio = len(vi) / max(1, len(raw))
    if len(raw) >= 24 and ratio < 0.34:
        signals.append("very_low_length_ratio")
    if len(raw) >= 45 and len(CLAUSES.findall(vi)) + 1 < max(2, (len(CLAUSES.findall(raw)) + 1) * 0.4):
        signals.append("clause_loss")
    if _balanced(raw) and not _balanced(vi):
        signals.append("unmatched_punctuation")
    if TERMINAL_SOURCE.search(raw.rstrip()) and not TERMINAL_VI.search(vi.rstrip()):
        signals.append("missing_terminal_punctuation")
    if FUNCTION_ENDING.search(vi.rstrip(" \t\n,;:")):
        signals.append("function_word_ending")
    return signals


def _segment_findings(identifier, raw, vi, dictionary, duplicate=False, check_truncation=True):
    findings = []
    vi = vi.strip()
    if not vi:
        return [{"kind": "empty_segment", "id": identifier}]
    if WRAPPERS.search(vi):
        findings.append({"kind": "malformed_wrapper", "id": identifier})
    if HAN.search(vi):
        findings.append({"kind": "chinese_residue", "id": identifier})
    if len(raw) > 80 and raw in vi:
        findings.append({"kind": "raw_copied", "id": identifier})
    if duplicate:
        findings.append({"kind": "duplicate_output", "id": identifier})
    mismatch = numeric_mismatch(raw, vi)
    if mismatch:
        findings.append({"kind": "suspicious_numeric_mismatch" if mismatch["ambiguous"]
                         else "numeric_mismatch", "id": identifier, **mismatch})
    for _, source, target in locked_matches(dictionary, raw):
        if not _locked_target_present(target, vi):
            findings.append({
                "kind": "locked_term_missing", "id": identifier,
                "source": source, "required": target,
            })
    signals = _truncation_signals(raw, vi) if check_truncation else []
    obvious = ("function_word_ending" in signals
               or (len(raw) >= 20 and "missing_terminal_punctuation" in signals))
    if obvious:
        findings.append({"kind": "obvious_truncation", "id": identifier,
                         "signals": signals})
    elif {"very_low_length_ratio", "unmatched_punctuation"}.intersection(signals) or len(signals) >= 2:
        findings.append({
            "kind": "suspicious_truncation", "id": identifier,
            "signals": signals, "ratio": round(len(vi) / max(1, len(raw)), 3),
        })
    return findings


def local_findings(chapter, result: Translation, dictionary, ratio_baseline=None,
                   paragraph_ratio_baseline=None):
    """Return deterministic defects and conservative semantic suspicions."""
    findings = []
    expected = chapter.paragraph_ids
    structural = structural_findings(expected, result.segments)
    if structural:
        return structural
    if chapter.title and not result.title.strip():
        findings.append({"kind": "empty_title", "id": chapter.title_id})
    raw_total = sum(len(text) for text in chapter.paragraphs)
    vi_total = sum(len(item.text) for item in result.segments)
    ratio = vi_total / max(1, raw_total)
    min_ratio = float(os.getenv("TRANSLATION_MIN_LENGTH_RATIO", "0.30"))
    if ratio < min_ratio or (ratio_baseline and len(ratio_baseline) >= 5
                             and ratio < sorted(ratio_baseline)[len(ratio_baseline) // 2] * 0.45):
        low_ids = [identifier for identifier, raw, segment in zip(
            expected, chapter.paragraphs, result.segments
        ) if len(segment.text) / max(1, len(raw)) < 0.40]
        for identifier in low_ids or list(expected):
            findings.append({"kind": "suspicious_length_ratio", "id": identifier,
                             "chapter_ratio": round(ratio, 3)})
    if paragraph_ratio_baseline and len(paragraph_ratio_baseline) >= 20:
        baseline = sorted(paragraph_ratio_baseline)[len(paragraph_ratio_baseline) // 2]
        for identifier, raw, segment in zip(expected, chapter.paragraphs, result.segments):
            paragraph_ratio = len(segment.text) / max(1, len(raw))
            if len(raw) >= 40 and paragraph_ratio < baseline * 0.35:
                findings.append({"kind": "suspicious_length_ratio", "id": identifier,
                                 "paragraph_ratio": round(paragraph_ratio, 3),
                                 "corpus_median": round(baseline, 3)})
    translated_counts = Counter(item.text.strip() for item in result.segments if item.text.strip())
    raw_by_output = {}
    for identifier, raw, segment in zip(expected, chapter.paragraphs, result.segments):
        duplicate = (len(segment.text.strip()) > 30 and translated_counts[segment.text.strip()] > 1
                     and raw_by_output.get(segment.text.strip(), raw) != raw)
        findings.extend(_segment_findings(identifier, raw, segment.text, dictionary, duplicate))
        raw_by_output.setdefault(segment.text.strip(), raw)
    if chapter.title:
        if TITLE_WRAPPER.match(result.title):
            findings.append({"kind": "malformed_title_wrapper", "id": chapter.title_id})
        findings.extend(_segment_findings(chapter.title_id, chapter.title, result.title,
                                          dictionary, check_truncation=False))
    unique = []
    seen = set()
    for finding in findings:
        key = (finding["kind"], finding.get("id"))
        if key not in seen:
            seen.add(key)
            unique.append(finding)
    return unique


def repair_integrity_findings(identifier, raw, original, candidate, dictionary):
    """Reject a fragment before it can replace a complete accepted paragraph."""
    is_title = identifier.startswith("C") and identifier.endswith("_TITLE")
    findings = _segment_findings(identifier, raw, candidate, dictionary,
                                 check_truncation=not is_title)
    candidate = candidate.strip()
    original = original.strip()
    if original and len(original) >= 20 and len(candidate) < max(12, int(len(original) * 0.55)):
        findings.append({
            "kind": "repair_content_loss", "id": identifier,
            "original_chars": len(original), "candidate_chars": len(candidate),
        })
    if _balanced(original) and not _balanced(candidate):
        findings.append({"kind": "repair_unbalanced_punctuation", "id": identifier})
    if TERMINAL_VI.search(original) and not TERMINAL_VI.search(candidate):
        findings.append({"kind": "repair_unterminated", "id": identifier})
    return findings


def semantic_suspicions(findings):
    return [finding for finding in findings if finding["kind"] in SEMANTIC_SUSPICIONS]


def deterministic_defects(findings):
    return [finding for finding in findings if finding["kind"] not in SEMANTIC_SUSPICIONS]


def source_quality_findings(chapters):
    """Record evident RAW damage without reconstructing or changing source text."""
    result = []
    for chapter in chapters:
        for identifier, raw in chapter.paragraph_items:
            if SOURCE_CENSORSHIP.search(raw):
                result.append({"chapter": chapter.number, "id": identifier,
                               "kind": "asterisk_censorship"})
            if not _balanced(raw):
                result.append({"chapter": chapter.number, "id": identifier,
                               "kind": "unbalanced_source_punctuation"})
    return result
