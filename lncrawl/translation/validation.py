"""Concrete local output findings; successful translations need no provider review."""

import re
from collections import Counter

from .dictionary import terminology_findings
from .models import Issue
from .parsing import HAN, parse_chapter_heading

PLACEHOLDER = re.compile(
    r"\{\{[^{}\n]+\}\}|\{[^{}\n]+\}|<[/\w][^<>\n]*>|%\([^)]+\)[sd]|\$\{[^}\n]+\}"
)


def _location(payload):
    location = payload.get("location") or {}
    return location.get("chapter"), location.get("chunk")


def _source_segment_id(payload, paragraph_id, paragraph=None):
    paragraph = paragraph or {}
    if paragraph.get("source_segment_id"):
        return paragraph["source_segment_id"]
    chapter, chunk = _location(payload)
    if chapter is not None and chunk is not None:
        return f"c{chapter}-k{chunk}-s{paragraph_id}"
    if chapter is not None:
        return f"c{chapter}-s{paragraph_id}"
    return f"s{paragraph_id}"


def _finding(
    payload,
    paragraph,
    *,
    kind,
    finding_type,
    reason,
    explanation,
    expected=None,
    actual=None,
    translated_excerpt=None,
    details=None,
    validator=None,
    **fields,
):
    paragraph = paragraph or {}
    chapter, chunk = _location(payload)
    source_text = paragraph.get("text", "")
    return Issue(
        segment_id=paragraph.get("id", -1),
        kind=kind,
        type=finding_type,
        reason=reason,
        explanation=explanation,
        severity="FATAL",
        validator=validator,
        chapter_number=chapter,
        chunk_index=chunk,
        source_segment_id=_source_segment_id(payload, paragraph.get("id", -1), paragraph),
        source_line=paragraph.get("source_line", paragraph.get("line")),
        source_excerpt=source_text or None,
        expected=expected,
        actual=actual,
        translated_excerpt=translated_excerpt,
        details=details,
        **fields,
    )


def _normalized_words(value):
    return [
        word.casefold()
        for word in re.findall(r"[\wÀ-ỹĐđ]+", value or "", re.UNICODE)
        if len(word) > 1
    ]


def _merged_output_region(payload, paragraph_id, segments):
    """Recognize a missing ID as merged only with visible VP support."""
    vp_by_id = {item.get("id"): item.get("text", "") for item in payload.get("vp", [])}
    expected_words = _normalized_words(vp_by_id.get(paragraph_id, ""))
    # One isolated word (often a name or a shared function word) cannot prove
    # that an entire source paragraph was merged into a neighbor.
    if len(expected_words) < 2:
        return None
    for segment in segments:
        output_words = set(_normalized_words(segment.text))
        matched = [word for word in expected_words if word in output_words]
        # A shared dictionary/name token is not proof that the missing source
        # was merged into this output paragraph.  Suppress a missing-ID
        # finding only when the complete aligned VP word set is visible;
        # partial overlap remains a real coverage failure.
        if len(matched) == len(expected_words):
            return {
                "output_segment_id": segment.id,
                "matched_words": matched,
                "coverage_status": "merged_with_neighbor",
            }
    return None


def validate_findings(issues):
    """Reject anonymous fatal findings before they reach logs or repair."""
    for issue in issues:
        if issue.severity != "FATAL":
            continue
        if not issue.type or not issue.validator:
            raise ValueError(
                "Validation finding invariant failed: fatal finding lacks "
                f"type/validator (segment={issue.source_segment_id!r})"
            )
        actionable = any(
            value not in (None, "", [], {})
            for value in (
                issue.source,
                issue.source_excerpt,
                issue.translated_excerpt,
                issue.actual_text,
                issue.details,
            )
        )
        if not actionable:
            raise ValueError(
                "Validation finding invariant failed: "
                f"validator={issue.validator!r}, type={issue.type!r}, "
                f"segment={issue.source_segment_id!r} has no actionable evidence"
            )
    return issues


def _deduplicate_findings(issues):
    """Keep one diagnostic for each concrete validator/source observation."""
    result, seen = [], set()
    for issue in issues:
        if issue.type == "terminology":
            key = (
                issue.type,
                issue.segment_id,
                issue.canonical_source,
                issue.source,
                issue.required_translation,
            )
        else:
            key = (
                issue.type,
                issue.segment_id,
                issue.reason,
                issue.expected,
                issue.actual,
            )
        if key in seen:
            continue
        seen.add(key)
        result.append(issue)
    return result


def local_findings(payload, translation):
    expected = [paragraph["id"] for paragraph in payload["raw"]]
    ids = [segment.id for segment in translation.segments]
    unknown = set(ids) - set(expected)
    if unknown:
        raise ValueError(
            f"Provider returned unexpected paragraph IDs {sorted(unknown)}; output cannot be paired safely"
        )
    issues = []
    missing_ids = set()
    if ids != expected:
        for paragraph_id in expected:
            paragraph = next(item for item in payload["raw"] if item["id"] == paragraph_id)
            if paragraph_id not in ids:
                missing_ids.add(paragraph_id)
                merged = _merged_output_region(payload, paragraph_id, translation.segments)
                if not merged:
                    issues.append(
                        _finding(
                            payload,
                            paragraph,
                            kind="missing",
                            finding_type="content_missing",
                            reason="missing_source_segment_translation",
                            explanation="RAW segment has no corresponding translated output segment",
                            expected="Translated content corresponding to this RAW segment",
                            actual="No matching translated segment found",
                            details={
                                "coverage_status": "missing",
                                "matching_output_region": None,
                                "coverage_score": 0.0,
                            },
                            validator="content_coverage",
                        )
                    )
            elif ids.count(paragraph_id) > 1:
                issues.append(
                    _finding(
                        payload,
                        paragraph,
                        kind="duplicate",
                        finding_type="duplicate_content",
                        reason="duplicate_output_segment_id",
                        explanation="Output contains the same source segment ID more than once",
                        expected="One translated segment for this source segment",
                        actual=str(ids.count(paragraph_id)),
                        details={"occurrences": ids.count(paragraph_id)},
                        validator="structure",
                    )
                )
        if len(ids) == len(expected) and set(ids) == set(expected):
            for index, paragraph_id in enumerate(expected):
                if ids[index] != paragraph_id:
                    issues.append(
                        _finding(
                            payload,
                            next(item for item in payload["raw"] if item["id"] == paragraph_id),
                            kind="order",
                            finding_type="ordering",
                            reason="source_order_mismatch",
                            explanation="Output paragraph order differs from RAW",
                            expected=str(expected),
                            actual=str(ids),
                            details={"expected_order": expected, "actual_order": ids},
                            validator="structure",
                        )
                    )
    segments = {segment.id: segment.text for segment in translation.segments}
    paragraphs = [{"id": -1, "text": payload.get("raw_title", "")}, *payload["raw"]]
    for paragraph in paragraphs:
        paragraph_id, raw = paragraph["id"], paragraph["text"]
        text = translation.title if paragraph_id == -1 else segments.get(paragraph_id, "")
        if paragraph_id in missing_ids:
            # The source-aware missing-ID finding above is the single report.
            continue
        if not text.strip():
            issues.append(
                _finding(
                    payload,
                    paragraph,
                    kind="missing",
                    finding_type="content_missing",
                    reason="missing_translation_region",
                    explanation="Translated output region is empty",
                    expected="A non-empty Vietnamese representation of the RAW segment",
                    actual="Translated output region is empty",
                    translated_excerpt="",
                    details={
                        "coverage_status": "missing",
                        "matching_output_region": None,
                        "coverage_score": 0.0,
                    },
                    validator="content_coverage",
                )
            )
            continue
        if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ufffd\ud800-\udfff]", text):
            issues.append(
                _finding(
                    payload,
                    paragraph,
                    kind="invented",
                    finding_type="format",
                    reason="invalid_output_characters",
                    explanation="Invalid Unicode/control characters in output",
                    actual=text,
                    translated_excerpt=text,
                    validator="format",
                )
            )
        residue = len(HAN.findall(text))
        if paragraph_id == -1:
            source_heading = parse_chapter_heading(raw)
            output_heading = parse_chapter_heading(text)
            if source_heading and output_heading and source_heading.number != output_heading.number:
                issues.append(
                    _finding(
                        payload,
                        paragraph,
                        kind="number",
                        finding_type="chapter_heading",
                        reason="chapter_number_mismatch",
                        explanation="Translated title changes the chapter number",
                        expected=str(source_heading.number),
                        actual=str(output_heading.number),
                        translated_excerpt=text,
                        validator="chapter_heading",
                    )
                )
        if paragraph_id == -1 and ("\n" in text.strip() or len(text) > max(200, len(raw) * 9)):
            issues.append(
                _finding(
                    payload,
                    paragraph,
                    kind="name",
                    finding_type="format",
                    reason="malformed_chapter_heading",
                    explanation="Malformed title: multiple lines or excessive length",
                    actual=text,
                    translated_excerpt=text,
                    validator="chapter_heading",
                )
            )
        if residue >= 4 or residue / max(1, len(text)) > 0.02:
            issues.append(
                _finding(
                    payload,
                    paragraph,
                    kind="missing",
                    finding_type="untranslated_chinese",
                    reason="untranslated_chinese_residue",
                    explanation="Output contains excessive untranslated Chinese residue",
                    expected="Vietnamese rendering without substantial Chinese residue",
                    actual=text,
                    translated_excerpt=text,
                    details={"chinese_character_count": residue},
                    validator="untranslated_chinese",
                )
            )
        han_count = len(HAN.findall(raw))
        if paragraph_id != -1 and (
            han_count >= 12 and len(text) < han_count * 0.4 or len(text) > max(200, len(raw) * 9)
        ):
            issues.append(
                _finding(
                    payload,
                    paragraph,
                    kind="missing",
                    finding_type="content_coverage",
                    reason="content_length_anomaly",
                    explanation="Suspicious translated/source length anomaly; verify complete RAW coverage",
                    expected="Complete semantic coverage of the RAW segment",
                    actual=text,
                    translated_excerpt=text,
                    details={
                        "coverage_status": "ambiguous",
                        "matching_output_region": {"output_segment_id": paragraph_id},
                        "raw_length": len(raw),
                        "translated_length": len(text),
                        "chinese_character_count": han_count,
                    },
                    validator="content_coverage",
                )
            )
        if Counter(PLACEHOLDER.findall(raw)) != Counter(PLACEHOLDER.findall(text)):
            issues.append(
                _finding(
                    payload,
                    paragraph,
                    kind="name",
                    finding_type="format",
                    reason="placeholder_mismatch",
                    explanation="Literal placeholders/markup changed or added",
                    expected=str(Counter(PLACEHOLDER.findall(raw))),
                    actual=str(Counter(PLACEHOLDER.findall(text))),
                    translated_excerpt=text,
                    validator="format",
                )
            )
        for finding in terminology_findings(
            payload.get("terminology", []), raw, text, payload.get("location")
        ):
            issues.append(
                _finding(
                    payload,
                    paragraph,
                    kind="terminology",
                    finding_type="terminology",
                    reason=finding["reason"],
                    explanation=(
                        f"RAW contains {finding['source']}; frozen mapping requires "
                        f"{finding['required_translation']!r}, but the translated text "
                        f"contains {finding['actual_text'] or 'no matching realization'!r} "
                        f"({finding['reason']})"
                    ),
                    actual=finding["actual_text"],
                    validator="terminology",
                    source=finding["source"],
                    matched_source=finding["matched_source"],
                    canonical_source=finding["canonical_source"],
                    required_translation=finding["required_translation"],
                    actual_text=finding["actual_text"],
                    start=finding["start"],
                    end=finding["end"],
                    left_context=finding["left_context"],
                    right_context=finding["right_context"],
                    context=finding["context"],
                    source_spans=finding["source_spans"],
                    source_occurrences=finding["source_occurrences"],
                    expected_occurrences=finding["expected_occurrences"],
                    matched_occurrences=finding["matched_occurrences"],
                    location=finding["location"],
                )
            )
    # Equal RAW repetitions are legitimate; unrelated long paragraphs repeated
    # verbatim are suspicious. Short recurring dialogue is deliberately exempt.
    owners = {}
    for paragraph in payload["raw"]:
        text = segments.get(paragraph["id"], "")
        if (
            len(paragraph["text"]) >= 40
            and text in owners
            and owners[text]["text"] != paragraph["text"]
        ):
            issues.append(
                _finding(
                    payload,
                    paragraph,
                    kind="duplicate",
                    finding_type="duplicate_content",
                    reason="duplicate_translated_content",
                    explanation="Unrelated RAW paragraphs have identical long output",
                    expected="Distinct source paragraphs should not share identical long output",
                    actual=text,
                    translated_excerpt=text,
                    details={"previous_source_excerpt": owners[text]["text"]},
                    validator="duplicate_content",
                )
            )
        owners[text] = paragraph
    return validate_findings(_deduplicate_findings(issues))
