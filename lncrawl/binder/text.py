"""Minimal text binder: one plain-text file, optionally with the novel header."""

import logging
from pathlib import Path
from typing import List, Optional

from ..core import Chapter, Novel
from .vietphrase import build_export_text, validate_export

logger = logging.getLogger(__name__)


def _configured_limit(name: str) -> Optional[int]:
    """UI setting for a safe-block limit, or ``None`` when unset.

    ``None`` keeps the environment variable visible: an explicit UI value wins,
    otherwise :func:`build_export_text` falls back to env/default.
    """
    try:
        from ..context import ctx
    except Exception:  # noqa: BLE001 - binder must stay usable without a context
        return None
    value = getattr(ctx.config, name, None)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def make_text(
    novel: Novel,
    chapters: List[Chapter],
    out_file: Path,
    *,
    target: Optional[int] = None,
    maximum: Optional[int] = None,
    include_header: bool = True,
    include_chapter_number: bool = False,
) -> Path:
    """Write one TXT file.

    ``include_header`` (default ``True``) keeps the novel metadata header in the
    file; split exports pass ``False`` for every file after the first one.
    """
    if target is None:
        target = _configured_limit("safe_block_target")
    if maximum is None:
        maximum = _configured_limit("safe_block_max")
    included = [chapter for chapter in chapters if chapter.success]
    built = build_export_text(
        novel,
        included,
        target=target,
        maximum=maximum,
        include_header=include_header,
        include_chapter_number=include_chapter_number,
    )
    validate_export(built)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_bytes(built.text.encode("utf-8"))
    audit = built.audit
    logger.info(
        "Created: %s (chapters=%d blocks=%d blanks=%d oversized=%d max=%d header=%s)",
        out_file,
        audit["chapters_exported"],
        audit["safe_blocks_created"],
        audit["blank_boundaries_inserted"],
        audit["oversized_paragraphs_split"],
        audit["maximum_block_chars"],
        audit["include_header"],
    )
    return out_file
