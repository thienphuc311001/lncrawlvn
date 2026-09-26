"""Detect chapter numbers already visible in the exported chapter heading."""

import re

from ..core import Chapter
from ..utils.html_tools import extract_text

_NUMBERED_HEADING = re.compile(
    r"^\s*(?:第\s*(?:\d+|[零〇一二三四五六七八九十百千万两]+)\s*[章回节卷篇]"
    r"|(?:Chương|Chapter)\s+\d+\b|#\s*\d+\b)",
    re.IGNORECASE,
)


def heading_has_number(heading: str) -> bool:
    return bool(_NUMBERED_HEADING.match(heading))


def has_chapter_number(chapter: Chapter, fmt: str) -> bool:
    """TXT shows the body; EPUB shows both the TOC title and the body."""
    if fmt == "epub" and heading_has_number(chapter.title or ""):
        return True
    body = extract_text(chapter.body or "")
    return heading_has_number(body)
