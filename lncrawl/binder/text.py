"""Minimal text binder: one plain-text file with the whole novel."""

import logging
from pathlib import Path
from typing import List

from ..core import Chapter, Novel
from ..utils.html_tools import extract_text

logger = logging.getLogger(__name__)


def make_text(
    novel: Novel,
    chapters: List[Chapter],
    out_file: Path,
) -> Path:
    included = [chapter for chapter in chapters if chapter.success]
    lines: List[str] = [
        novel.title or "",
        f"by {novel.author}" if novel.author else "",
        "",
        "-" * 60,
        "",
        extract_text(novel.synopsis or ""),
        "",
        f"Source: {novel.url}",
        f"Tags: {', '.join(novel.tags or [])}",
        f"Volumes: {len(novel.volumes)}",
        f"Chapters: {len(included)}",
        "",
        "+" * 60,
        "",
    ]

    for chapter in included:
        lines += [
            f"Chapter {chapter.id}: {chapter.title}",
            "-" * 60,
            "",
            extract_text(chapter.body or ""),
            "",
            "",
        ]

    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text("\n".join(lines), encoding="utf-8")
    logger.info(f"Created: {out_file}")
    return out_file
