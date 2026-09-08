# -*- coding: utf-8 -*-
import base64
import logging
import re
import time
from typing import TYPE_CHECKING, Optional
from urllib.parse import urlparse

from scraper import Action, Blocked, Diagnosis, Layer

from lncrawl.core import Chapter, Novel, PageSoup, SoupTemplate

if TYPE_CHECKING:
    from requests import Response

logger = logging.getLogger(__name__)

# Novel URLs arrive on the desktop host, but that edge spends the client's
# request window after ~15 chapter pages and then refuses everything for
# hours — an HTTP 403 访问太频繁 page, or a 200 出错了 shell that only
# redirects back to itself. The mobile edge serves the same book, the same
# chapter ids and the same split pages, under a window that drains in
# minutes. Every page is therefore fetched through it; the desktop host is
# kept in base_url only so its URLs still resolve to this crawler.
MOBILE_HOST = "https://m.1qxs.com"

# Novel page: /xs_1/{bookId}.html — the xs_N segment is the site channel.
# The mobile mirror drops the .html suffix.
NOVEL_PATH = re.compile(r"/(xs_\d+)/(\d+)(?:\.html)?/?$")
# Chapter or split-page link: /xs_1/{bookId}/{chapterId}, with or without a
# .html suffix on the desktop host, and /xs_1/{bookId}/{chapterId}/{pageNo}
# for the split pages.
CHAPTER_LINK = re.compile(r"/xs_\d+/(\d+)/(\d+)(?:\.html)?")
# Chapter titles carry their split-page counter: 第1章 收租，然后遇见医学奇迹(1/5)
SPLIT_SUFFIX = re.compile(r"\s*[（(]\d+/\d+[)）]\s*$")
# Promo line repeated as the first paragraph of every split page.
PROMO_TEXT = re.compile(r"小说免费阅读|1qxs\.com")
# The visible half of a mobile chapter page ends with a teaser saying the
# chapter cannot be fully shown here; the hidden half travels in p_key.
TEASER_TEXT = re.compile(r"阅\|读\|模\|式|加\|载\|更\|多")
# The hidden remainder of a mobile split page: base64 in an inline script,
# decoded and appended to div.content by the site's own reader (content.min.js:
# atob(p_key) -> TextDecoder -> .content.append). Decoding it here reproduces
# the page a reader sees, without a JavaScript runtime.
P_KEY = re.compile(r"p_key='([^']+)'")
# Both 200 shells carry 出错了 in the header bar. A spent request window
# embeds a setTimeout redirect back to the same URL; a missing chapter id
# says 页面不存在 and never comes back, so only the first is worth waiting for.
ERROR_SHELL_MARKER = 'class="title">出错了'
ERROR_SHELL_NOT_FOUND = "页面不存在"
# The mobile edge answers a spent window with HTTP 403 and a 访问太频繁 body.
# A bare 403 carries no vendor signalling, so the scraper's generic detector
# cannot attribute it, every tier refuses, and get_soup raises Blocked
# instead of backing off — the refusal never reaches check_response. It is
# ridden out where it surfaces, in _read_page. Minutes, not the 30 seconds
# the page promises: the window keeps refusing well past its own copy.
BLOCKED_RETRY_AFTER_SECONDS = 180.0
MAX_BLOCKED_ATTEMPTS = 5
# What the source tells the scraper to wait when the redirecting shell shows
# up; long enough for the window to drain, short enough to keep a walk moving.
SHELL_RETRY_AFTER_SECONDS = 90.0
# Chapter ids are mostly dense, with a few deleted ones in between; a
# not-found shell answers instantly, so this many in a row means the ids no
# longer belong to this book (or every page is being refused), not a gap.
MAX_CONSECUTIVE_MISSES = 10


class YiqiNovelCrawler(SoupTemplate):
    """Crawler for 一七小说 (www.1qxs.com) — Chinese web novels.

    Four things make this site different from a stock SoupTemplate fit:

    1. The chapter-list pages (/catalog_1/{bookId}.html and its mobile
       mirror) answer with the site's 出错了 shell even in a real browser,
       and there is no JSON API behind them. But chapter ids are dense
       integers from 1, so the list is rebuilt by walking
       /xs_1/{bookId}/{chapterId} up to the newest id linked on the novel
       page. A missing id in the middle (a deleted chapter) is skipped.
    2. Long chapters are split across {chapterId}, {chapterId}/2, ...
       chained by the 下一页 link — which on the last split points at the
       next chapter, so the walk is bounded by comparing chapter ids. On
       the mobile pages each split shows only its first half in the DOM;
       the rest travels base64-encoded in a p_key script variable and is
       decoded here the way the site's own reader does.
    3. The edge enforces a small request window per client, with two
       refusal modes. A spent window may serve a 200 error shell that
       redirects to itself — caught in check_response and reported as a
       BACKOFF so the scraper waits out the window. It may also answer
       HTTP 403 with a 访问太频繁 body, which the scraper's generic
       detector cannot attribute and refuses outright; that variant is
       ridden out in _read_page instead.
    4. The desktop host's window drains over hours, the mobile host's in
       minutes, so every page is fetched through m.1qxs.com (see
       MOBILE_HOST) at a pace the mobile window sustains.
    """

    base_url = [
        "https://www.1qxs.com/",
        "https://m.1qxs.com/",
    ]
    language = "zh"
    # Measured against the mobile edge: a burst of ~20-25 chapter requests
    # spends the window, while one page every ~10 seconds runs indefinitely.
    # Pace under the sustained rate and let the retries ride out the rest.
    request_rate_limit = 0.1
    # The edge's request window is shared by the whole process, so parallel
    # downloads just multiply the pace past it: every worker draws its own
    # gap from the per-origin pacer, and three workers at a 10s interval
    # arrive as one request every ~3s. One worker keeps the walk, the
    # downloads and the pacer telling the same story about one reader.
    max_sessions_per_exit = 1

    @classmethod
    def max_workers(cls) -> int:
        return 1

    novel_title_selector = "h1"
    novel_synopsis_selector = "p#in-details"
    chapter_body_selector = "div.content"

    def initialize(self) -> None:
        self.cleaner.bad_css.update({"p#prompt"})
        # One combined pattern, not a list of them: the cleaner renders list
        # items with an f-string, which would turn a compiled regex into its
        # repr, so a list of patterns never matches anything. A single
        # alternation covering both the promo paragraph and the
        # reading-mode teaser at the end of every split page.
        self.cleaner.bad_tag_text_pairs.update(
            {"p": re.compile(f"{PROMO_TEXT.pattern}|{TEASER_TEXT.pattern}")}
        )
        # The crawler is opened on the desktop origin, where Sources taught the
        # pacer this source's rate limit — but every page is fetched through
        # the mobile host, and pacing is keyed by host. Teach the mobile
        # clock the same interval or it runs at the default 3s and spends
        # the window within the first ~25 pages. Both the pacer and the
        # persisted profile: fetch() re-learns the interval from the profile
        # on every call, so a stale value there would stomp this one.
        mobile_profile = self.scraper.memory.profile(MOBILE_HOST)
        mobile_profile.interval = 1.0 / self.request_rate_limit
        self.scraper.memory.touch()
        self.scraper.pacer.learn(mobile_profile.origin, mobile_profile.interval)

    # ------------------------------------------------------------------ #
    # Refusals the scraper's own detectors cannot see
    # ------------------------------------------------------------------ #

    def check_response(self, response: "Response", body: str) -> Optional[Diagnosis]:
        # The redirecting 出错了 shell is the site's own way of saying "not
        # now": keep it inside the scraper's loop so the window is waited out
        # there. The 页面不存在 shell is a permanent answer about one id and
        # must be returned as-is; _read_page reads it as a missing page.
        if ERROR_SHELL_MARKER in body and ERROR_SHELL_NOT_FOUND not in body:
            return Diagnosis(
                action=Action.BACKOFF,
                layer=Layer.WORKERS,
                detail="the site's edge served its 出错了 shell instead of the page",
                retry_after=SHELL_RETRY_AFTER_SECONDS,
            )
        return None

    # ------------------------------------------------------------------ #
    # Novel page
    # ------------------------------------------------------------------ #

    def get_novel_soup(self, novel: Novel) -> PageSoup:
        page = self._read_page(self._mobile_novel_url(novel.url))
        if page is None:
            raise Exception(f"Novel page not readable at {novel.url}")
        return page

    def parse_cover(self, soup: PageSoup, novel: Novel) -> None:
        # The mobile page lazy-loads the cover; the real URL sits in
        # data-original rather than src (which is a placeholder).
        img = soup.select_one(".book-intro .cover img")
        if img:
            for attr in ("data-original", "data-src", "src"):
                src = img.get(attr)
                if src:
                    novel.cover_url = self.absolute_url(str(src))
                    return

    def parse_authors(self, soup: PageSoup, novel: Novel) -> None:
        # The byline is a plain div inside the book header. Scoped: the page's
        # recommendation cards further down carry div.author of their own.
        author = soup.select_one(".book-intro .author")
        if author:
            novel.author = author.text.strip()

    def parse_tags(self, soup: PageSoup, novel: Novel) -> None:
        # div.type reads "轻小说&nbsp;…&nbsp;恋爱日常": the anchor is the
        # category and the trailing text nodes are genres. The entity
        # separators do not survive the parser (the text nodes come out
        # glued to the anchor's text), so the anchor is lifted out first
        # and what remains is one text node per genre — no splitting
        # needed. Scoped like the author line: the recommendation cards
        # further down have a div.type of their own.
        type_block = soup.select_one(".book-intro .type")
        if not type_block:
            return
        categories = [a.text.strip() for a in type_block.select("a[href]") if a.text.strip()]
        for anchor in type_block.select("a[href]"):
            anchor.extract()
        genres = [text.strip() for text in type_block.text.split() if text.strip()]
        novel.tags = categories + genres

    def parse_summary(self, soup: PageSoup, novel: Novel) -> None:
        # The desktop page carried the blurb in og:description; the mobile
        # page only has it in p#in-details, prefixed with a boilerplate
        # sentence of the form "书名小说由一七小说提供精彩免费全文阅读：" —
        # the blurb starts after that colon.
        tag = soup.select_one(self.novel_synopsis_selector)
        if not tag:
            return
        text = " ".join(tag.text.split())
        head, sep, blurb = text.partition("：")
        if sep and "免费" in head and "小说" in head:
            text = blurb
        novel.synopsis = text.strip()

    # ------------------------------------------------------------------ #
    # Chapter list
    # ------------------------------------------------------------------ #

    def parse_toc(self, soup: PageSoup, novel: Novel) -> None:
        book_id, latest_id = self._latest_chapter(soup, novel)
        if latest_id < 1:
            raise Exception(f"No chapters found for {novel.url}")
        channel = self._novel_channel(novel)
        consecutive_misses = 0
        for chapter_id in range(1, latest_id + 1):
            url = f"{MOBILE_HOST}/{channel}/{book_id}/{chapter_id}"
            page = self._read_page(url)
            title = self._page_title(page) if page else None
            if title is None:
                # A deleted chapter (or a doubly-refused page); the ids are
                # dense enough that skipping keeps the rest of the book.
                consecutive_misses += 1
                if consecutive_misses > MAX_CONSECUTIVE_MISSES:
                    raise Exception(f"{consecutive_misses} consecutive chapters missing from {url}")
                logger.warning(f"Skipping missing chapter id {chapter_id} of book {book_id}")
                continue
            consecutive_misses = 0
            novel.add_chapter(title=title, url=url)

    # ------------------------------------------------------------------ #
    # Chapter body
    # ------------------------------------------------------------------ #

    def download_chapter(self, chapter: Chapter) -> None:
        chapter_id = self._chapter_id(chapter.url)
        parts: list[str] = []
        url = chapter.url
        while url:
            page = self._read_page(url)
            if page is None:
                raise Exception(f"Chapter page not found at {url}")
            body = page.select_one(self.chapter_body_selector)
            if body:
                parts.append(body.inner_html)
            hidden = self._hidden_text(page)
            if hidden:
                parts.append(hidden)
            url = self._next_page_url(page, chapter_id)
        if not parts:
            raise Exception(f"Empty chapter body at {chapter.url}")
        combined = PageSoup.create(f"<div>{''.join(parts)}</div>")
        self.parse_chapter_body(combined.select_one("div"), chapter)

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _mobile_novel_url(self, novel_url: str) -> str:
        match = NOVEL_PATH.search(urlparse(novel_url).path)
        if not match:
            raise Exception(f"Not a novel page URL: {novel_url}")
        channel, book_id = match.group(1), match.group(2)
        return f"{MOBILE_HOST}/{channel}/{book_id}"

    def _latest_chapter(self, soup: PageSoup, novel: Novel) -> tuple[int, int]:
        """Return (bookId, latestChapterId) from the novel page."""
        match = NOVEL_PATH.search(urlparse(novel.url).path)
        book_id = int(match.group(2)) if match else 0
        best = 0
        # The novel page links the newest chapters with their ids; the
        # largest one bounds the walk below. (The desktop page also carries
        # og:novel:latest_chapter_url, but the mobile page does not.)
        for anchor in soup.select("a[href]"):
            link = CHAPTER_LINK.search(str(anchor.get("href") or ""))
            if link and int(link.group(1)) == book_id:
                best = max(best, int(link.group(2)))
        if book_id < 1 or best < 1:
            raise Exception(f"No chapter links on {novel.url}")
        return book_id, best

    def _novel_channel(self, novel: Novel) -> str:
        match = NOVEL_PATH.search(urlparse(novel.url).path)
        return match.group(1) if match else "xs_1"

    def _chapter_id(self, url: str) -> int:
        link = CHAPTER_LINK.search(urlparse(url).path)
        if not link:
            raise Exception(f"Not a chapter page URL: {url}")
        return int(link.group(2))

    def _read_page(self, url: str) -> Optional[PageSoup]:
        """Fetch one page through the mobile edge, riding out spent windows.

        get_soup already rides out the redirecting 出错了 shell through
        check_response's BACKOFF. The 403 访问太频繁 variant escapes the
        scraper as a Blocked error instead, so it is waited out here: widen
        the shared pacing interval for this host, sleep out the window, and
        fetch the same page again.
        """
        key = self.scraper.memory.key(url)
        for attempt in range(MAX_BLOCKED_ATTEMPTS):
            try:
                page = self.scraper.get_soup(url)
            except Blocked as e:
                wait = BLOCKED_RETRY_AFTER_SECONDS * (attempt + 1)
                widened = self.scraper.pacer.throttled(key, wait)
                logger.warning(
                    f"Request window spent on {url} (attempt {attempt + 1} of "
                    f"{MAX_BLOCKED_ATTEMPTS}); waiting {widened:.0f}s: {e}"
                )
                time.sleep(widened)
                continue
            except Exception as e:
                logger.warning(f"Failed to fetch {url}: {e}")
                return None
            # Real pages carry an h1 (novel title or chapter title); both
            # error shells carry none. A shell is a final answer about this
            # url — the not-found one permanently, the window one already
            # had its retries inside get_soup.
            if page is not None and page.select_one("h1"):
                return page
            return None
        return None

    def _page_title(self, page: PageSoup) -> Optional[str]:
        title_tag = page.select_one("h1")
        if not title_tag:
            return None
        return SPLIT_SUFFIX.sub("", title_tag.text.strip())

    def _hidden_text(self, page: PageSoup) -> str:
        """The base64 remainder of a split page, as the site's reader decodes it."""
        match = P_KEY.search(page.body.inner_html)
        if not match:
            return ""
        try:
            return base64.b64decode(match.group(1)).decode("utf-8")
        except Exception as e:
            logger.warning(f"Could not decode the hidden part of a page: {e}")
            return ""

    def _next_page_url(self, page: PageSoup, chapter_id: int) -> Optional[str]:
        # The 下一页 link serves two roles: the next split page of this
        # chapter, or — from the last split — the next chapter. Only a link
        # that keeps the same chapter id continues the walk.
        for anchor in page.select("div.page a[href]"):
            if anchor.text.strip() != "下一页":
                continue
            href = str(anchor.get("href") or "").strip()
            link = CHAPTER_LINK.search(href)
            if not link or int(link.group(2)) != chapter_id:
                return None
            return self.absolute_url(href)
        return None
