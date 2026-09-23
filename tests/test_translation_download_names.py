"""Download names follow the uploaded RAW batch without renaming checkpoints."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lncrawl.translation import api
from lncrawl.translation.store import Store


class DownloadNameTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_output_uses_batch_stem_for_each_download(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root, inputs={
                "raw": "第141章 开始\n你好。", "dictionary": None,
                "source_name": "0141-0150.txt", "book_title": "0141-0150",
            })
            store.progress("done", stage="Complete")
            names = {
                "translated.txt": "0141-0150-translated.txt",
                "translated.json": "0141-0150-translated.json",
                "dictionary.json": "0141-0150-dictionary.json",
                "unresolved.json": "0141-0150-unresolved.json",
                "author-notes.txt": "0141-0150-author-notes.txt",
            }
            for filename in names:
                store.write(filename, "content" if filename.endswith(".txt") else {})
            with patch.object(api, "ROOT", root):
                snapshot = api.snapshot(store)
                self.assertEqual(snapshot["output_filenames"]["translation"], names["translated.txt"])
                self.assertEqual(snapshot["output_filenames"]["dictionary"], names["dictionary.json"])
                for filename, expected in names.items():
                    response = await api.output(store.id, filename)
                    self.assertEqual(response.filename, expected)
                    self.assertTrue((store.path / filename).exists())

    async def test_preserved_outputs_keep_batch_download_name(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root, inputs={
                "raw": "第1章 开始\n你好。", "dictionary": None,
                "source_name": r"C:\uploads\Truyện mới.txt",
            })
            store.progress("done", stage="Complete")
            store.write("translated.txt", "Bản dịch")
            with patch.object(api, "ROOT", root):
                await api.delete_job(store.id)
                response = await api.output(store.id, "translated.txt")
            self.assertEqual(response.filename, "Truyện-mới-translated.txt")
            self.assertTrue(Path(response.path).is_file())

    def test_untrusted_filename_is_sanitized(self):
        self.assertEqual(api._download_stem(
            {"source_name": "../my/story: vol 1?.txt"}, "a" * 64), "story-vol-1")
        self.assertEqual(api._download_stem({}, "a" * 64), "translation-aaaaaaaa")
        long_name = api._download_names({"source_name": "界" * 200 + ".txt"}, "a" * 64)
        self.assertLess(len(long_name["author_notes"].encode("utf-8")), 255)
