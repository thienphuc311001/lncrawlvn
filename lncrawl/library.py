"""On-disk novel library backing the web UI's Books tab.

Layout (under ``APP_DIR/library``)::

    <book_id>/book.json                      metadata + full chapter TOC
    <book_id>/cover.jpg                      optional downloaded cover
    <book_id>/chapters/0001-0100/ch_0001.json  one file per fetched chapter
    <book_id>/exports/<title>.epub.zip       cached export bundles

Chapters are written to disk the moment their download finishes, so a crashed
job, dead server, or flaky network never loses already-fetched content. A
later "fetch missing" run only downloads chapters that have no file yet.
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


class Library:
    """Filesystem-backed store of crawled novels."""

    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = Path(root) if root else APP_DIR / "library"

    # ------------------------------------------------------------------ #
    # Paths
    # ------------------------------------------------------------------ #

    def _book_dir(self, book_id: str) -> Path:
        return self.root / book_id

    def chapter_rel_path(self, chapter_id: int) -> str:
        """Relative file path for a chapter, grouped 100 per numbered folder."""
        start = ((chapter_id - 1) // CHAPTERS_PER_FOLDER) * CHAPTERS_PER_FOLDER + 1
        end = start + CHAPTERS_PER_FOLDER - 1
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

    def export_zip(self, book_id: str, fmt: str = "epub") -> Path:
        """Build an EPUB/TXT from saved chapters and bundle it into a ZIP."""
        if fmt not in ("epub", "txt"):
            raise LNException(f"Unsupported export format: {fmt}")
        novel, chapters = self._load_novel_and_chapters(book_id)

        exports_dir = self._book_dir(book_id) / "exports"
        exports_dir.mkdir(parents=True, exist_ok=True)
        stem = safe_filename(novel.title or "") or book_id

        if fmt == "epub":
            cover_file = self._book_dir(book_id) / "cover.jpg"
            target = make_epub(
                novel,
                chapters,
                exports_dir / f"{stem}.epub",
                cover_file if cover_file.is_file() else None,
            )
        else:
            target = make_text(novel, chapters, exports_dir / f"{stem}.txt")

        zip_path = target.with_suffix(target.suffix + ".zip")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(target, arcname=target.name)
        logger.info("Export bundle: %s", zip_path)
        return zip_path


LIBRARY = Library()
