"""Concrete local output findings; successful translations need no provider review."""

import re
from collections import Counter

from .dictionary import terminology_findings
from .models import Issue
from .parsing import HAN, HEADING, chapter_number

PLACEHOLDER = re.compile(
    r"\{\{[^{}\n]+\}\}|\{[^{}\n]+\}|<[/\w][^<>\n]*>|%\([^)]+\)[sd]|\$\{[^}\n]+\}"
)


def local_findings(payload, translation):
    expected = [paragraph["id"] for paragraph in payload["raw"]]
    ids = [segment.id for segment in translation.segments]
    unknown = set(ids) - set(expected)
    if unknown:
        raise ValueError(
            f"Provider returned unexpected paragraph IDs {sorted(unknown)}; output cannot be paired safely"
        )
    issues = []
    if ids != expected:
        for paragraph_id in expected:
            kind = (
                "missing"
                if paragraph_id not in ids
                else "duplicate"
                if ids.count(paragraph_id) > 1
                else None
            )
            if kind:
                issues.append(
                    Issue(
                        segment_id=paragraph_id,
                        kind=kind,
                        explanation="Output IDs must cover each RAW paragraph exactly once in source order",
                    )
                )
        if len(ids) == len(expected) and set(ids) == set(expected):
            for index, paragraph_id in enumerate(expected):
                if ids[index] != paragraph_id:
                    issues.append(
                        Issue(
                            segment_id=paragraph_id,
                            kind="order",
                            explanation="Output paragraph order differs from RAW",
                        )
                    )
    segments = {segment.id: segment.text for segment in translation.segments}
    paragraphs = [{"id": -1, "text": payload.get("raw_title", "")}, *payload["raw"]]
    for paragraph in paragraphs:
        paragraph_id, raw = paragraph["id"], paragraph["text"]
        text = translation.title if paragraph_id == -1 else segments.get(paragraph_id, "")
        if not text.strip():
            issues.append(
                Issue(
                    segment_id=paragraph_id,
                    kind="missing",
                    explanation="Empty translated title/paragraph",
                )
            )
            continue
        if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ufffd\ud800-\udfff]", text):
            issues.append(
                Issue(
                    segment_id=paragraph_id,
                    kind="invented",
                    explanation="Invalid Unicode/control characters in output",
                )
            )
        residue = len(HAN.findall(text))
        if paragraph_id == -1:
            source_heading, output_heading = HEADING.match(raw), HEADING.match(text)
            if (
                source_heading
                and output_heading
                and chapter_number(source_heading[1] or source_heading[2])
                != chapter_number(output_heading[1] or output_heading[2])
            ):
                issues.append(
                    Issue(
                        segment_id=-1,
                        kind="number",
                        explanation="Translated title changes the chapter number",
                    )
                )
        if paragraph_id == -1 and ("\n" in text.strip() or len(text) > max(200, len(raw) * 9)):
            issues.append(
                Issue(
                    segment_id=-1,
                    kind="name",
                    explanation="Malformed title: multiple lines or excessive length",
                )
            )
        if residue >= 4 or residue / max(1, len(text)) > 0.02:
            issues.append(
                Issue(
                    segment_id=paragraph_id,
                    kind="missing",
                    explanation="Excessive untranslated Chinese residue",
                )
            )
        han_count = len(HAN.findall(raw))
        if paragraph_id != -1 and (
            han_count >= 12 and len(text) < han_count * 0.4 or len(text) > max(200, len(raw) * 9)
        ):
            issues.append(
                Issue(
                    segment_id=paragraph_id,
                    kind="missing",
                    explanation="Suspicious translated/source length anomaly; verify complete RAW coverage",
                )
            )
        if Counter(PLACEHOLDER.findall(raw)) != Counter(PLACEHOLDER.findall(text)):
            issues.append(
                Issue(
                    segment_id=paragraph_id,
                    kind="name",
                    explanation="Literal placeholders/markup changed or added",
                )
            )
        for finding in terminology_findings(
            payload.get("terminology", []), raw, text, payload.get("location")
        ):
            issues.append(
                Issue(
                    segment_id=paragraph_id,
                    kind="terminology",
                    explanation=(
                        f"RAW contains {finding['source']}; frozen mapping requires "
                        f"{finding['required_translation']!r}, but the translated text "
                        f"contains {finding['actual_text'] or 'no matching realization'!r} "
                        f"({finding['reason']})"
                    ),
                    type=finding["type"],
                    source=finding["source"],
                    matched_source=finding["matched_source"],
                    canonical_source=finding["canonical_source"],
                    required_translation=finding["required_translation"],
                    actual_text=finding["actual_text"],
                    reason=finding["reason"],
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
                Issue(
                    segment_id=paragraph["id"],
                    kind="duplicate",
                    explanation="Unrelated RAW paragraphs have identical long output",
                )
            )
        owners[text] = paragraph
    return issues
