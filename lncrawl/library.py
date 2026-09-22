"""On-disk novel library backing the web UI's Books tab.

Layout (under ``APP_DIR/library``)::

    <book_id>/book.json                      metadata + full chapter TOC
    <book_id>/cover.jpg                      optional downloaded cover
    <book_id>/chapters/0001-0100/ch_0001.json  one file per fetched chapter
    <book_id>/exports/0001-0100.epub           one file per export chunk
    <book_id>/exports/<title>.epub.zip         bundled export download

Chapters are written to disk the moment their download finishes, so a crashed
job, dead server, or flaky network never loses already-fetched content. A
later "fetch missing" run only downloads chapters that have no file yet.

Storage always groups chapters 100 per folder (``CHAPTERS_PER_FOLDER``), while
an export can pick its own number of chapters per file (``per_file``).
"""

import json
import logging
import shutil
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .binder import make_epub, make_text
from .context import APP_DIR
from .core import Chapter, Novel
from .exceptions import LNException
from .utils.file_tools import atomic_write, safe_filename

logger = logging.getLogger(__name__)

CHAPTERS_PER_FOLDER = 100
# Exports default to the on-disk folder size and accept anything from one
# chapter per file up to this ceiling (a book of N chapters yields at most
# N files, so the cap only guards against nonsense values).
DEFAULT_EXPORT_CHUNK = CHAPTERS_PER_FOLDER
MAX_EXPORT_CHUNK = 10000


class Library:
    """Filesystem-backed store of crawled novels."""

    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = Path(root) if root else APP_DIR / "library"

    # ------------------------------------------------------------------ #
    # Paths
    # ------------------------------------------------------------------ #

    def _book_dir(self, book_id: str) -> Path:
        return self.root / book_id

    def _chunk_bounds(self, chapter_id: int, per_file: int) -> Tuple[int, int]:
        """First and last chapter id of the export chunk holding a chapter."""
        start = ((chapter_id - 1) // per_file) * per_file + 1
        return start, start + per_file - 1

    def _folder_bounds(self, chapter_id: int) -> Tuple[int, int]:
        """First and last chapter id of the 100-chapter folder holding a chapter."""
        return self._chunk_bounds(chapter_id, CHAPTERS_PER_FOLDER)

    def chapter_rel_path(self, chapter_id: int) -> str:
        """Relative file path for a chapter, grouped 100 per numbered folder."""
        start, end = self._folder_bounds(chapter_id)
        return f"chapters/{start:04d}-{end:04d}/ch_{chapter_id:04d}.json"

    def _chapter_path(self, book_id: str, chapter_id: int) -> Path:
        return self._book_dir(book_id) / self.chapter_rel_path(chapter_id)

    def cover_path(self, book_id: str) -> Path:
        return self._book_dir(book_id) / "cover.jpg"

    def _guarded_book_dir(self, book_id: str) -> Path:
        """Book dir after verifying it stays inside the library root.

        Book ids arrive from the API path, so anything that would escape the
        root (``..``, absolute paths, nested ids) is an error rather than a
        cleanup target.
        """
        book_dir = self._book_dir(book_id)
        root = self.root.resolve()
        try:
            resolved = book_dir.resolve()
            inside_root = resolved.is_relative_to(root) and resolved != root
        except (OSError, ValueError):
            raise LNException(f"Invalid book id: {book_id}")
        if not inside_root:
            raise LNException(f"Invalid book id: {book_id}")
        return book_dir

    def delete_book(self, book_id: str) -> bool:
        """Remove a book with all chapters, cover, and exports.

        Returns True when something was deleted, False when the book dir did
        not exist.
        """
        book_dir = self._guarded_book_dir(book_id)
        if not book_dir.is_dir():
            return False
        shutil.rmtree(book_dir)
        logger.info("Deleted book from library: %s", book_id)
        return True

    def delete_chapter(self, book_id: str, chapter_id: int) -> bool:
        """Delete one saved chapter file, keeping the TOC entry in ``book.json``.

        The chapter immediately counts as missing again, so a later
        ``fetch missing`` run can re-download it from the source URL kept in
        the TOC. Cover, exports, and other chapters are untouched. Returns
        False when there was no saved file to delete.
        """
        path = self._guarded_book_dir(book_id) / self.chapter_rel_path(chapter_id)
        if not path.is_file():
            return False
        path.unlink()
        logger.info("Deleted chapter %s of book %s", chapter_id, book_id)
        return True

    # ------------------------------------------------------------------ #
    # Writes
    # ------------------------------------------------------------------ #

    def save_book_meta(self, meta: Dict[str, Any]) -> str:
        """Persist ``book.json`` and return the derived book id."""
        book_id = safe_filename(str(meta.get("title") or "")) or "novel"
        meta = {**meta, "book_id": book_id, "saved_at": time.time()}
        meta_path = self._book_dir(book_id) / "book.json"
        with atomic_write(meta_path, "w") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        return book_id

    def save_chapter(
        self, book_id: str, chapter: Dict[str, Any], overwrite: bool = False
    ) -> bool:
        """Write one chapter body; returns True when the file was written.

        With ``overwrite`` an existing saved copy is replaced (used by
        re-crawls that repair previously truncated bodies) and True is still
        returned. Without it an existing file is kept as-is and False is
        returned — the default first-crawl behavior.
        """
        chapter_id = int(chapter.get("id") or 0)
        if chapter_id <= 0:
            return False
        path = self._chapter_path(book_id, chapter_id)
        if path.is_file() and not overwrite:  # keep the earlier copy
            return False
        payload = {
            "id": chapter_id,
            "title": chapter.get("title") or "",
            "url": chapter.get("url") or "",
            "body": chapter.get("body") or "",
            "fetched_at": time.time(),
        }
        with atomic_write(path, "w") as f:
            json.dump(payload, f, ensure_ascii=False)
        return True

    # ------------------------------------------------------------------ #
    # Reads
    # ------------------------------------------------------------------ #

    def _read_meta(self, book_id: str) -> Optional[Dict[str, Any]]:
        meta_path = self._book_dir(book_id) / "book.json"
        if not meta_path.is_file():
            return None
        try:
            return json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            logger.warning("Corrupted book.json in %s", book_id)
            return None

    def list_books(self) -> List[Dict[str, Any]]:
        books: List[Dict[str, Any]] = []
        if not self.root.is_dir():
            return books
        for entry in sorted(self.root.iterdir()):
            if not entry.is_dir():
                continue
            meta = self._read_meta(entry.name)
            if meta is None:
                continue
            toc = meta.get("toc") or []
            saved = sum(1 for c in toc if self._chapter_path(entry.name, int(c["id"])).is_file())
            books.append(
                {
                    "book_id": entry.name,
                    "title": meta.get("title") or entry.name,
                    "author": meta.get("author") or "",
                    "cover_url": meta.get("cover_url") or "",
                    "total_chapters": len(toc),
                    "saved_count": saved,
                    "saved_at": meta.get("saved_at") or 0.0,
                }
            )
        books.sort(key=lambda b: b["saved_at"], reverse=True)
        return books

    def load_book(self, book_id: str) -> Optional[Dict[str, Any]]:
        """Metadata plus the TOC annotated with per-chapter availability."""
        meta = self._read_meta(book_id)
        if meta is None:
            return None
        toc = meta.get("toc") or []
        chapters = [
            {
                "id": int(c["id"]),
                "title": c.get("title") or "",
                "url": c.get("url") or "",
                "saved": self._chapter_path(book_id, int(c["id"])).is_file(),
            }
            for c in toc
        ]
        return {
            "book_id": book_id,
            "url": meta.get("url") or "",
            "title": meta.get("title") or book_id,
            "author": meta.get("author") or "",
            "cover_url": meta.get("cover_url") or "",
            "language": meta.get("language"),
            "synopsis": meta.get("synopsis") or "",
            "tags": meta.get("tags") or [],
            "total_chapters": len(chapters),
            "saved_count": sum(1 for c in chapters if c["saved"]),
            "chapters": chapters,
        }

    def load_chapter(self, book_id: str, chapter_id: int) -> Optional[Dict[str, Any]]:
        path = self._chapter_path(book_id, chapter_id)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            logger.warning("Corrupted chapter file: %s", path)
            return None

    def missing_chapter_ids(self, book_id: str) -> List[int]:
        book = self.load_book(book_id)
        if book is None:
            raise LNException(f"Book not found in library: {book_id}")
        return [c["id"] for c in book["chapters"] if not c["saved"]]

    # ------------------------------------------------------------------ #
    # Export
    # ------------------------------------------------------------------ #

    def _load_novel_and_chapters(self, book_id: str) -> Tuple[Novel, List[Chapter]]:
        book = self.load_book(book_id)
        if book is None:
            raise LNException(f"Book not found in library: {book_id}")
        novel = Novel(
            url=book["url"],
            title=book["title"],
            author=book["author"],
            cover_url=book["cover_url"],
            language=book.get("language"),
            synopsis=book["synopsis"],
            tags=list(book["tags"]),
        )
        chapters: List[Chapter] = []
        for entry in book["chapters"]:
            data = self.load_chapter(book_id, entry["id"])
            if data is None:
                continue
            chapters.append(
                Chapter(
                    id=data["id"],
                    url=data.get("url") or "",
                    title=data.get("title") or "",
                    body=data.get("body") or "",
                    success=True,
                )
            )
        if not chapters:
            raise LNException("No chapters saved yet — fetch the book first")
        return novel, chapters

    def _group_saved(
        self, chapters: List[Chapter], per_file: int
    ) -> List[Tuple[int, int, List[Chapter]]]:
        """Split saved chapters into export chunks of ``per_file`` chapters.

        Boundaries follow chapter ids (1-``per_file``, ``per_file``+1-2×
        ``per_file``, …), which is exactly the on-disk 100-chapter folder layout
        at the default size. Returns ``(start, end, chapters_in_chunk)`` tuples in
        chunk order, skipping any chunk without a saved chapter.
        """
        groups: List[Tuple[int, int, List[Chapter]]] = []
        for chapter in chapters:
            if not chapter.success:
                continue
            start, end = self._chunk_bounds(chapter.id, per_file)
            if not groups or groups[-1][0] != start:
                groups.append((start, end, []))
            groups[-1][2].append(chapter)
        return groups

    def _clear_exports(self, exports_dir: Path, stem: str, fmt: str) -> None:
        """Remove earlier export files of one book/format.

        Chapter-per-file is chosen per export, so filenames change between runs;
        stale copies are deleted instead of accumulating in ``exports/``. Chunk
        files carry no title — they are named after their chapter range only —
        so every direct file ending in ``.{fmt}`` is stale, including copies
        written by older builds that prefixed the title. The bundle itself ends
        in ``.{fmt}.zip`` and is removed separately. Files of other formats are
        never touched.
        """
        suffix = f".{fmt}"
        for path in exports_dir.iterdir():
            if path.is_file() and path.name.endswith(suffix):
                path.unlink(missing_ok=True)
        (exports_dir / f"{stem}.{fmt}.zip").unlink(missing_ok=True)

    def export_zip(
        self,
        book_id: str,
        fmt: str = "epub",
        per_file: int = DEFAULT_EXPORT_CHUNK,
    ) -> Path:
        """Build one EPUB/TXT per chapter chunk and bundle them into a ZIP.

        ``per_file`` is the number of chapters per output file (default 100,
        mirroring the on-disk folder ranges). Files are named after their chapter
        ranges only (``0001-0010.epub``, ``0011-0020.epub``, …); a file holding a
        single chapter drops the range (``0007.txt``). Only the bundle keeps the
        novel title.

        Only the first file — the one holding the earliest chapters, so the one
        containing chapter 1 when it is saved — carries the novel's front matter
        (metadata header, intro page, cover); the remaining files are pure
        chapters.
        """
        if fmt not in ("epub", "txt"):
            raise LNException(f"Unsupported export format: {fmt}")
        per_file = int(per_file)
        if not 1 <= per_file <= MAX_EXPORT_CHUNK:
            raise LNException(
                f"Chapters per file must be between 1 and {MAX_EXPORT_CHUNK}, "
                f"got {per_file}"
            )
        novel, chapters = self._load_novel_and_chapters(book_id)
        groups = self._group_saved(chapters, per_file)

        exports_dir = self._book_dir(book_id) / "exports"
        exports_dir.mkdir(parents=True, exist_ok=True)
        stem = safe_filename(novel.title or "") or book_id

        cover = self._book_dir(book_id) / "cover.jpg"
        cover = cover if cover.is_file() else None

        self._clear_exports(exports_dir, stem, fmt)

        targets: List[Path] = []
        for index, (start, end, chunk_chapters) in enumerate(groups):
            first = index == 0
            label = f"{start:04d}" if start == end else f"{start:04d}-{end:04d}"
            out = exports_dir / f"{label}.{fmt}"
            if fmt == "epub":
                make_epub(
                    novel,
                    chunk_chapters,
                    out,
                    cover if first else None,
                    include_intro=first,
                )
            else:
                make_text(novel, chunk_chapters, out, include_header=first)
            targets.append(out)

        zip_path = exports_dir / f"{stem}.{fmt}.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for target in targets:
                zf.write(target, arcname=target.name)
        logger.info(
            "Export bundle (%d files, %d chapters per file): %s",
            len(targets),
            per_file,
            zip_path,
        )
        return zip_path


LIBRARY = Library()
