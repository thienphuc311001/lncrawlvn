"""Regression tests for the EPUB export split.

A split export keeps the novel's front matter — the intro page (title, author,
synopsis, source) and the cover page — in the first file only, so every later
file is pure chapters.
"""

import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

from lncrawl.binder import make_epub
from lncrawl.core import Chapter, Novel
from lncrawl.library import Library
from lncrawl.utils.file_tools import safe_filename

STEM = safe_filename("测试小说")
COVER_BYTES = b"\xff\xd8\xff\xe0fake-jpeg"


def novel() -> Novel:
    return Novel(
        url="https://example.test/book",
        title="测试小说",
        author="作者",
        synopsis="简介。",
        tags=["历史"],
    )


def chapter(cid: int) -> Chapter:
    return Chapter(
        id=cid,
        url=f"https://example.test/ch/{cid}",
        title=f"第{cid}章",
        body=f"<h3>第{cid}章</h3><p>正文{cid}。</p>",
        success=True,
    )


def names_in(epub_path: Path):
    """Entry names inside one EPUB (which is itself a ZIP)."""
    with zipfile.ZipFile(epub_path) as archive:
        return archive.namelist()


def has(names, suffix: str) -> bool:
    return any(name.endswith(suffix) for name in names)


def unpack(data: bytes, target: Path):
    target.write_bytes(data)
    return names_in(target)


class EpubIntroTests(unittest.TestCase):
    def test_intro_and_cover_are_optional(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            cover = root / "cover.jpg"
            cover.write_bytes(COVER_BYTES)
            with_intro = names_in(
                make_epub(novel(), [chapter(1)], root / "with.epub", cover)
            )
            without_intro = names_in(
                make_epub(novel(), [chapter(1)], root / "without.epub", cover, include_intro=False)
            )

        self.assertTrue(has(with_intro, "intro.xhtml"))
        self.assertTrue(has(with_intro, "cover.jpg"))
        self.assertFalse(has(without_intro, "intro.xhtml"))
        self.assertFalse(has(without_intro, "cover.jpg"))
        for names in (with_intro, without_intro):
            self.assertTrue(has(names, "chapter_00001.xhtml"))


class LibraryEpubExportTests(unittest.TestCase):
    def test_split_export_keeps_intro_in_first_file(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            library = Library(root=root / "library")
            book_id = library.save_book_meta(
                {
                    "title": "测试小说",
                    "url": "https://example.test/book",
                    "author": "作者",
                    "synopsis": "简介。",
                    "tags": ["历史"],
                    "toc": [
                        {"id": cid, "title": f"第{cid}章", "url": f"https://example.test/ch/{cid}"}
                        for cid in (1, 2, 3)
                    ],
                }
            )
            library.cover_path(book_id).write_bytes(COVER_BYTES)
            for cid in (1, 2, 3):
                library.save_chapter(
                    book_id,
                    {
                        "id": cid,
                        "title": f"第{cid}章",
                        "url": f"https://example.test/ch/{cid}",
                        "body": f"<h3>第{cid}章</h3><p>正文{cid}。</p>",
                    },
                )

            zip_path = library.export_zip(book_id, "epub", per_file=2)
            self.assertEqual(zip_path.name, f"{STEM}.epub.zip")
            with zipfile.ZipFile(zip_path) as bundle:
                entries = sorted(n for n in bundle.namelist() if n.endswith(".epub"))
                self.assertEqual(entries, ["0001-0002.epub", "0003-0004.epub"])
                first = unpack(bundle.read(entries[0]), root / "first.epub")
                second = unpack(bundle.read(entries[1]), root / "second.epub")

        self.assertTrue(has(first, "intro.xhtml"))
        self.assertTrue(has(first, "cover.jpg"))
        self.assertFalse(has(second, "intro.xhtml"))
        self.assertFalse(has(second, "cover.jpg"))
        self.assertTrue(has(first, "chapter_00002.xhtml"))
        self.assertTrue(has(second, "chapter_00003.xhtml"))


if __name__ == "__main__":
    unittest.main()
