# -*- coding: utf-8 -*-
"""Crawler for 全本免费小说 (www.qbmfxs.com) — Chinese web novels."""
import base64
import logging
import re
from typing import Optional

from lncrawl.core import Chapter, Novel, PageSoup, SoupTemplate
from lncrawl.exceptions import LNException

logger = logging.getLogger(__name__)

# Chapter or split-page link: /book_1/{bookId}/{chapterId}.html, and
# /book_1/{bookId}/{chapterId}/{pageNo}.html for splits after the first.
# Group 2 is the chapter id; group 3 only exists on later splits.
CHAPTER_LINK = re.compile(r"/book_\d+/(\d+)/(\d+)(?:/(\d+))?\.html")
# The hidden remainder of a split page travels base64 in an inline p_key
# variable; the site's own reader decodes it and appends it to div.content.
P_KEY = re.compile(r"p_key='([^']+)'")
# Upper bound on the splits walked for one chapter. Real chapters are far
# smaller; the cap only guards against a changed layout making the chain
# circular.
MAX_SPLITS = 100
# The novel page's description div closes with a "reprinted work" recap line;
# it is plain text separated by <br>, so the paragraph-based cleaner rules
# cannot reach it and it is dropped here instead.
SUMMARY_BOILERPLATE = re.compile(r"<p>[^<]*小说为转载作品[^<]*</p>")


class QbmFxsCrawler(SoupTemplate):
    """Crawler for 全本免费小说 — Chinese web novels.

    Three things make this site different from a stock SoupTemplate fit:

    1. The novel page (`/book_1/{bookId}.html`) only renders the newest
       chapters; the full catalog is a separate page linked as 查看完整目录,
       so the TOC is parsed from `/list_1/{bookId}.html` instead.
    2. Long chapters are split across `{chapterId}.html`, `{chapterId}/2`,
       `{chapterId}/3`, ... chained by the 下一页 link, which on the last
       split points at the next chapter instead — so the walk is bounded by
       comparing chapter ids.
    3. Each split page shows only its first half in the DOM; the rest
       travels base64-encoded in a `p_key` script variable and is decoded
       here the way the site's own reader does.

    Bare requests answer with the site's 网络错误 shell; a browser visit to
    any first page plants the `uid`/`refresh_token` cookies the catalog
    requires, and the internal Referer the reader sketch serves with comes
    from the scraper's navigation chain automatically.
    """

    base_url = ["https://www.qbmfxs.com/"]
    language = "zh"

    novel_title_selector = "div.book div.detail div.name h1"
    novel_tags_selector = (
        'div.detail .label a[href*="/store/"], '
        'div.detail .label span.tags a'
    )
    novel_synopsis_selector = "div.bookinfo div.desc div.description"

    def initialize(self) -> None:
        # Drop whole paragraphs that are site furniture rather than story: the
        # promo line every split page opens with, the "switch your browser"
        # prompt every split ends with, and the "continued on the next page"
        # teaser that closes each base64 remainder.
        self.cleaner.bad_tag_text_pairs["p"] = [
            "小说免费阅读，请收藏",
            "更换谷歌浏览器即可正常阅读",
            "本章未完，点击",
        ]

    def parse_authors(self, soup: PageSoup, novel: Novel) -> None:
        # The detail span reads "兴霸天  著"; strip the role suffix.
        tag = soup.select_one("div.book div.detail div.name span")
        if tag:
            novel.author = re.sub(r"[\s\xa0]*著\s*$", "", tag.text).strip()

    def parse_summary(self, soup: PageSoup, novel: Novel) -> None:
        # The blurb lives in a plain-text div closed by a "reprinted work"
        # recap line; drop that line and fall back to the OpenGraph description
        # when the section is missing entirely.
        tag = soup.select_one(self.novel_synopsis_selector)
        text = self.cleaner.extract_contents(tag)
        novel.synopsis = SUMMARY_BOILERPLATE.sub("", text).strip()
        if not novel.synopsis:
            meta_tag = soup.select_one(SoupTemplate.novel_synopsis_selector)
            content = str(meta_tag.get("content") or "") if meta_tag else ""
            holder = PageSoup.create(f"<p>{content}</p>").select_one("p")
            novel.synopsis = self.cleaner.extract_contents(holder)

    def parse_toc(self, soup: PageSoup, novel: Novel) -> None:
        # The novel page renders only the newest chapters; the full catalog is
        # a separate page linked as 查看完整目录.
        link = soup.select_one('a[href*="/list_1/"]')
        if not link:
            raise LNException("No catalog link found")
        catalog = self.scraper.get_soup(self.absolute_url(link["href"]))
        for anchor in catalog.select("div.catalog div.list ul li a"):
            title = anchor.select_one("p.line_1") or anchor
            novel.add_chapter(
                title=" ".join(title.text.split()),
                url=self.absolute_url(anchor["href"]),
            )

    def download_chapter(self, chapter: Chapter) -> None:
        url = self.build_chapter_url(chapter)
        link = CHAPTER_LINK.search(url)
        if not link:
            raise LNException(f"Not a chapter page URL: {url}")
        chapter_id = int(link.group(2))

        # Concatenate every split's rendered HTML, then clean once at the end
        # so the site-furniture patterns also cover the decoded halves.
        raws = []
        current = url
        for _ in range(MAX_SPLITS):
            page = self.scraper.get_soup(current)
            body = page.select_one("div.read div.content") or page.select_one("div.content")
            if not body:
                raise LNException(f"No chapter body found at {current}")
            raws.append(body.outer_html)
            raws.append(self._hidden_html(page))
            current = self._next_page_url(page, chapter_id, current)
            if current is None:
                break
        else:
            logger.warning(f"Split walk exceeded {MAX_SPLITS} pages for {url}")

        assembled = self.scraper.make_soup(f"<div>{''.join(raws)}</div>")
        chapter.body = self.cleaner.extract_contents(assembled.select_one("div"))

    def _hidden_html(self, page: PageSoup) -> str:
        """The base64 remainder of a split page, as the site's reader decodes it."""
        match = P_KEY.search(page.body.inner_html)
        if not match:
            return ""
        try:
            return base64.b64decode(match.group(1)).decode("utf-8")
        except Exception as e:
            logger.warning(f"Could not decode the hidden part of a page: {e}")
            return ""

    def _next_page_url(
        self, page: PageSoup, chapter_id: int, current: str
    ) -> Optional[str]:
        """The next split of this chapter, or None when the chapter ends.

        The 下一页 link chains split pages; on the last split it points at the
        next chapter instead, so the walk stops at the first link whose chapter
        id differs.
        """
        anchor = page.select_one("a#next[href]")
        if not anchor:
            return None
        href = str(anchor.get("href") or "").strip()
        if not href or href.startswith("javascript") or href == "#":
            return None
        next_url = self.absolute_url(href)
        if next_url == current:
            return None
        link = CHAPTER_LINK.search(next_url)
        if not link or int(link.group(2)) != chapter_id:
            return None
        return next_url
