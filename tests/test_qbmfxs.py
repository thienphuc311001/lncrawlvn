"""Regression coverage for the qbmfxs chapter reader's split-page chain."""

import base64
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from scraper import PageSoup

from lncrawl.core import Chapter
from lncrawl.core.cleaner import TextCleaner
from sources.zh.qbmfxs import QbmFxsCrawler


BOOK = "https://www.qbmfxs.com/book_1/93316"
MOBILE = "https://m.qbmfxs.com/book_1/93316"


def split_page(visible: str, hidden: str, next_path: str) -> PageSoup:
    encoded = base64.b64encode(f"<p>{hidden}</p>".encode()).decode()
    return PageSoup.create(
        '<div class="read"><div class="content">'
        f'<p>{visible}</p><p>加|载|更|多</p></div></div>'
        '<div class="page"><div class="right">'
        f'<a href="{next_path}">下一页</a></div></div>'
        f"<script>var p_key='{encoded}';</script>"
    )


class QbmFxsSplitTests(unittest.TestCase):
    def test_downloads_all_three_splits_and_stops_before_next_chapter(self):
        pages = {
            BOOK + "/1": split_page("第一段", "第一段后半", "/book_1/93316/1/2"),
            MOBILE + "/1/2": split_page("第二段", "第二段后半", "/book_1/93316/1/3"),
            MOBILE + "/1/3": split_page("第三段", "第三段后半", "/book_1/93316/2"),
        }
        crawler = object.__new__(QbmFxsCrawler)
        crawler.scraper = SimpleNamespace(last_url=MOBILE + "/1", make_soup=PageSoup.create)
        crawler.cleaner = TextCleaner()
        crawler.initialize()
        chapter = Chapter(id=1, url=BOOK + "/1.html", title="难道我是神？")

        with patch.object(crawler, "_read_page", side_effect=pages.__getitem__) as fetch:
            crawler.download_chapter(chapter)

        self.assertEqual(
            chapter.body,
            "<p>第一段</p><p>第一段后半</p>"
            "<p>第二段</p><p>第二段后半</p>"
            "<p>第三段</p><p>第三段后半</p>",
        )
        self.assertEqual(fetch.call_count, 3)


if __name__ == "__main__":
    unittest.main()
