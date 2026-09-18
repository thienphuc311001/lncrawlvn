"""Regression tests for the TXT export chapter structure.

Downloaded/exported TXT must not contain the synthetic ``Chapter N: <title>``
wrapper that used to be printed from the chapter-list title. Every chapter is a
separator followed by the crawled body, whose first line is the authoritative
native chapter heading.
"""

import re
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

from lncrawl.binder import make_text
from lncrawl.core import Chapter, Novel
from lncrawl.library import Library

SEP = "-" * 60
WRAPPER_LINE = re.compile(r"^Chapter \d+[:：]", re.MULTILINE)


def novel() -> Novel:
    return Novel(
        url="https://example.test/book",
        title="测试小说",
        author="作者",
        synopsis="简介。",
        tags=["历史"],
    )


def chapter(cid, wrapper_title, native_heading, paragraphs):
    """A crawled chapter: list title in ``title``, real heading inside the body."""
    body = f"<h3>{native_heading}</h3>" + "".join(f"<p>{p}</p>" for p in paragraphs)
    return Chapter(
        id=cid,
        url=f"https://example.test/ch/{cid}",
        title=wrapper_title,
        body=body,
        success=True,
    )


def export(chapters):
    with TemporaryDirectory() as tmp:
        out = Path(tmp) / "book.txt"
        make_text(novel(), chapters, out)
        return out.read_text(encoding="utf-8")


def chapter_blocks(text):
    """Chapter line-blocks in order, each without its leading separator."""
    region = text.split("+" * 60, 1)[1]
    parts = region.split("\n" + SEP + "\n")
    return [part.strip("\n").split("\n") for part in parts[1:]]


class ExportHeadingTests(unittest.TestCase):
    def test_wrapper_and_native_heading_identical(self):
        text = export([chapter(1, "第1章 开始", "第1章 开始", ["正文。"])])
        self.assertEqual(chapter_blocks(text), [["第1章 开始", "正文。"]])
        self.assertNotRegex(text, WRAPPER_LINE)

    def test_wrapper_shorter_than_native_heading(self):
        native = "第102章 大明关键岗位人员看板，可视化管理系统"
        text = export(
            [chapter(102, "第102章 大明关键岗位人员看板", native, ["高启愚搞了个大新闻..."])]
        )
        self.assertEqual(chapter_blocks(text), [[native, "高启愚搞了个大新闻..."]])
        self.assertNotIn("第102章 大明关键岗位人员看板\n", text)
        self.assertNotRegex(text, WRAPPER_LINE)

    def test_wrapper_differs_in_punctuation(self):
        native = "第66章 收获记忆碎片（4k字）"
        text = export([chapter(66, "第66章 收获记忆碎片(4K字)", native, ["正文。"])])
        self.assertEqual(chapter_blocks(text), [[native, "正文。"]])
        self.assertIn("（4k字）", text)
        self.assertNotIn("(4K字)", text)

    def test_native_heading_has_extra_words(self):
        native = "第5章 危机嗅觉觉醒的第一天"
        text = export([chapter(5, "第5章 危机", native, ["他终于明白了。"])])
        self.assertEqual(chapter_blocks(text), [[native, "他终于明白了。"]])
        self.assertNotIn("第5章 危机\n", text)

    def test_multiple_chapters_sequential_order(self):
        chapters = [
            chapter(n, f"第{n}章 标题{n}", f"第{n}章 标题{n}", [f"第{n}章正文。"])
            for n in (101, 102, 103)
        ]
        text = export(chapters)
        blocks = chapter_blocks(text)
        self.assertEqual(len(blocks), 3)
        self.assertEqual(
            [block[0] for block in blocks],
            ["第101章 标题101", "第102章 标题102", "第103章 标题103"],
        )
        self.assertEqual(
            [block[1] for block in blocks],
            ["第101章正文。", "第102章正文。", "第103章正文。"],
        )
        self.assertNotRegex(text, WRAPPER_LINE)

    def test_no_chapter_body_line_removed(self):
        paragraphs = ["第一段。", "第二段，含标点！", "第三段。"]
        text = export([chapter(7, "第7章 标题", "第7章 标题", paragraphs)])
        block = chapter_blocks(text)[0]
        self.assertEqual(block, ["第7章 标题"] + paragraphs)
        for paragraph in paragraphs:
            self.assertEqual(text.count(paragraph), 1)

    def test_separator_before_every_chapter(self):
        chapters = [chapter(n, f"第{n}章 标题", f"第{n}章 标题", ["正文。"]) for n in (1, 2, 3)]
        text = export(chapters)
        region = text.split("+" * 60, 1)[1]
        self.assertEqual(region.count(SEP + "\n"), 3)
        for heading in ("第1章 标题", "第2章 标题", "第3章 标题"):
            self.assertIn(SEP + "\n" + heading + "\n", text)

    def test_no_generated_chapter_wrapper_anywhere(self):
        text = export(
            [
                chapter(1, "第1章 开始", "第1章 开始", ["正文。"]),
                chapter(2, "第2章 继续", "第2章 继续", ["正文。"]),
            ]
        )
        self.assertNotRegex(text, WRAPPER_LINE)
        for line in text.splitlines():
            self.assertFalse(line.startswith("Chapter "), line)

    def test_task_sample_heading_not_merged_and_utf8_preserved(self):
        text = export(
            [
                chapter(101, "第101章 明摄宗张居正", "第101章 明摄宗张居正", ["正文第一段..."]),
                chapter(
                    102,
                    "第102章 大明关键岗位人员看板,可视化管理",
                    "第102章 大明关键岗位人员看板，可视化管理系统",
                    ["高启愚搞了个大新闻..."],
                ),
            ]
        )
        self.assertIn(
            SEP + "\n第102章 大明关键岗位人员看板，可视化管理系统\n高启愚搞了个大新闻...",
            text,
        )
        self.assertIn(SEP + "\n第101章 明摄宗张居正\n正文第一段...", text)
        # The wrapper's truncated, half-width-comma title must not survive as a
        # line of its own (the native heading still ends with 可视化管理系统).
        self.assertNotIn("看板,可视化管理", text)
        self.assertNotIn("可视化管理\n", text)
        self.assertNotIn("Chapter 102", text)
        self.assertNotIn("Chapter 101", text)


class LibraryTextExportTests(unittest.TestCase):
    def test_cached_chapters_export_to_txt(self):
        entries = [
            (101, "第101章 明摄宗张居正", "第101章 明摄宗张居正", ["正文第一段..."]),
            (
                102,
                "第102章 大明关键岗位人员看板,可视化管理",
                "第102章 大明关键岗位人员看板，可视化管理系统",
                ["高启愚搞了个大新闻..."],
            ),
        ]
        with TemporaryDirectory() as tmp:
            library = Library(root=Path(tmp) / "library")
            book_id = library.save_book_meta(
                {
                    "title": "测试小说",
                    "url": "https://example.test/book",
                    "author": "作者",
                    "toc": [
                        {"id": cid, "title": wrapper, "url": f"https://example.test/ch/{cid}"}
                        for cid, wrapper, _, _ in entries
                    ],
                }
            )
            for cid, wrapper, native, paragraphs in entries:
                saved = library.save_chapter(
                    book_id,
                    {
                        "id": cid,
                        "title": wrapper,
                        "url": f"https://example.test/ch/{cid}",
                        "body": f"<h3>{native}</h3>"
                        + "".join(f"<p>{p}</p>" for p in paragraphs),
                    },
                )
                self.assertTrue(saved)

            zip_path = library.export_zip(book_id, "txt")
            with zipfile.ZipFile(zip_path) as archive:
                names = [n for n in archive.namelist() if n.endswith(".txt")]
                self.assertEqual(len(names), 1)
                text = archive.read(names[0]).decode("utf-8")

        self.assertNotRegex(text, WRAPPER_LINE)
        blocks = chapter_blocks(text)
        self.assertEqual(
            [block[0] for block in blocks],
            [
                "第101章 明摄宗张居正",
                "第102章 大明关键岗位人员看板，可视化管理系统",
            ],
        )
        self.assertEqual(blocks[1][1], "高启愚搞了个大新闻...")


if __name__ == "__main__":
    unittest.main()
