"""DocLN catalog and protected chapter regressions."""

import base64
import json
import unittest
from unittest.mock import patch

from scraper import PageSoup

from lncrawl.core import Novel
from sources.vi.lnhakone import ListNovelCrawler


BOOK = "https://docln.net/truyen/21180-vinh-thoai-hiep-si"
FIRST = BOOK + "/c627139-chuong-00-ngay-moi"
SECOND = BOOK + "/c162820-chuong-01-uoc-mo-cua-ta-la-hiep-si"


def encoded_chunk(index, html, key):
    data = html.encode("utf-8")
    encrypted = bytes(byte ^ key[i % len(key)] for i, byte in enumerate(data))
    return f"{index:04d}" + base64.b64encode(encrypted).decode("ascii")


class HakoCrawlerTests(unittest.TestCase):
    def setUp(self):
        self.crawler = ListNovelCrawler(origin="https://docln.net/")

    def tearDown(self):
        self.crawler.close()

    def test_catalog_preserves_chapter_order_skips_empty_volumes_and_reads_metadata(self):
        page = PageSoup.create('''
            <span class="series-name"><a href="/truyen/21180-vinh-thoai-hiep-si">Vĩnh Thoái Hiệp Sĩ</a></span>
            <div class="series-cover"><div class="img-in-ratio" style="background-image: url('https://i2.hako.vip/cover.webp')"></div></div>
            <div class="series-summary"><div class="summary-content"><p>Encrid mơ làm <strong>hiệp sĩ</strong>.</p><p>Hắn vẫn kiên cường.</p></div></div>
            <div class="series-gernes"><a href="/the-loai/fantasy">Fantasy</a></div>
            <section class="volume-list"><span class="sect-title">Chương 01 - 100</span>
              <ul class="list-chapters"><li><a href="/truyen/21180-vinh-thoai-hiep-si/c627139-chuong-00-ngay-moi" title="Chương 00 - Ngày mới">Chương 00</a></li>
              <li><a href="/truyen/21180-vinh-thoai-hiep-si/c162820-chuong-01-uoc-mo-cua-ta-la-hiep-si">Chương 01 - Ước mơ của ta là Hiệp sĩ</a></li></ul>
            </section>
            <section class="volume-list"><span class="sect-title">Chương 101 - 200</span><ul class="list-chapters"></ul></section>
            <section class="volume-list"><span class="sect-title">Chương 201 - 300</span>
              <ul class="list-chapters"><li><a href="/truyen/21180-vinh-thoai-hiep-si/c3" title="Chương 201">Chương 201</a></li></ul>
            </section>
        ''')
        with patch.object(self.crawler, "get_soup", return_value=page):
            novel = Novel(url=BOOK)
            self.crawler.read_novel(novel)

        self.assertEqual(novel.title, "Vĩnh Thoái Hiệp Sĩ")
        self.assertEqual(novel.cover_url, "https://i2.hako.vip/cover.webp")
        self.assertEqual(novel.synopsis, "Encrid mơ làm hiệp sĩ.\nHắn vẫn kiên cường.")
        self.assertEqual(novel.tags, ["Fantasy"])
        self.assertEqual([v.title for v in novel.volumes], ["Chương 01 - 100", "Chương 201 - 300"])
        self.assertEqual([c.url for c in novel.chapters], [FIRST, SECOND, BOOK + "/c3"])
        self.assertEqual([c.volume for c in novel.chapters], [1, 1, 2])
        self.assertEqual(novel.chapters[1].title, "Chương 01 - Ước mơ của ta là Hiệp sĩ")

    def test_xor_shuffled_body_keeps_prose_and_illustration_not_hidden_heading_or_ads(self):
        key = "b4a6a02cf80b54f5".encode()
        chunks = [
            encoded_chunk(1, '<p>Đèn lồng.</p><p><img src="https://i2.hako.vip/illustration.jpg"/></p>', key),
            encoded_chunk(0, "<p>Người thầy đầu tiên dạy kiếm cho Encrid.</p>", key),
        ]
        payload = json.dumps(chunks)
        page = PageSoup.create(f'''
            <div id="chapter-content">
              <p style="display: none">Chương 01 - Ước mơ của ta là Hiệp sĩ</p>
              <div id="chapter-c-protected" data-s="xor_shuffle" data-k="{key.decode()}"
                   data-c='{payload}'></div>
              <a href="/truyen/8476"><img src="https://i2.hako.vip/banner.jpg"></a>
            </div>
        ''')
        with patch.object(self.crawler, "get_soup", return_value=page):
            body = self.crawler.download_chapter_body({"url": SECOND})
        self.assertEqual(
            body,
            '<p>Người thầy đầu tiên dạy kiếm cho Encrid.</p><p>Đèn lồng.</p>'
            '<p><img src="https://i2.hako.vip/illustration.jpg"/></p>',
        )

    def test_empty_or_unknown_protection_fails_instead_of_saving_a_blank_chapter(self):
        for scheme, payload in (("xor_shuffle", "[]"), ("unknown", '["0000YQ=="]')):
            with self.subTest(scheme=scheme, payload=payload):
                page = PageSoup.create(
                    f'<div id="chapter-content"><p style="display: none">Chương 01</p>'
                    f'<div id="chapter-c-protected" data-s="{scheme}" data-k="secret" '
                    f"data-c='{payload}'></div></div>"
                )
                with patch.object(self.crawler, "get_soup", return_value=page):
                    with self.assertRaises(ValueError):
                        self.crawler.download_chapter_body({"url": SECOND})


if __name__ == "__main__":
    unittest.main()
