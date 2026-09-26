import json
import tempfile
import unittest
from pathlib import Path

from lncrawl.exceptions import LNException
from lncrawl.library import Library


class LibraryTitleTranslationTests(unittest.TestCase):
    def test_updates_toc_and_saved_chapter_titles_without_changing_bodies(self):
        with tempfile.TemporaryDirectory() as directory:
            library = Library(root=Path(directory) / "library")
            book_id = library.save_book_meta({
                "title": "Novel",
                "toc": [{"id": 1, "title": "原题", "url": "https://example.test/1"}],
            })
            library.save_chapter(book_id, {"id": 1, "title": "原题", "body": "chapter body"})

            library.update_chapter_titles(book_id, {1: "Tiêu đề mới"})

            self.assertEqual(library.load_book(book_id)["chapters"][0]["title"], "Tiêu đề mới")
            saved = library.load_chapter(book_id, 1)
            self.assertEqual(saved["title"], "Tiêu đề mới")
            self.assertEqual(saved["body"], "chapter body")

    def test_rejects_titles_for_chapters_not_in_the_book(self):
        with tempfile.TemporaryDirectory() as directory:
            library = Library(root=Path(directory) / "library")
            book_id = library.save_book_meta({"title": "Novel", "toc": [{"id": 1, "title": "原题"}]})

            with self.assertRaises(LNException):
                library.update_chapter_titles(book_id, {2: "wrong chapter"})

            with (library.root / book_id / "book.json").open(encoding="utf-8") as metadata:
                self.assertEqual(json.load(metadata)["toc"][0]["title"], "原题")


if __name__ == "__main__":
    unittest.main()
