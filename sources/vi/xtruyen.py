# -*- coding: utf-8 -*-
import base64
import logging
import re
import time
import zlib
from typing import List, Optional
from urllib.parse import urlencode

from lncrawl.core import Chapter, Novel, PageSoup, Volume
from lncrawl.exceptions import LNException
from lncrawl.templates.madara import MadaraTemplate

logger = logging.getLogger(__name__)

# Chapter bodies arrive inside the page as a `data_x` literal: base64 written in a
# custom alphabet, which the site's own script translates index-by-index into the
# standard alphabet before `atob`, then zlib-inflates into the chapter HTML.
_CUSTOM_B64 = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ-_"
_STANDARD_B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
_B64_TRANS = str.maketrans(_CUSTOM_B64, _STANDARD_B64)

# The chapter-list API rejects requests that do not carry this header. It is a
# static key embedded in the theme's obfuscated script, not a per-session token.
_API_AUTH_HEADER = "abC0000011111"

# The API answers at most ~200 chapters per call, so the list is fetched in windows.
_CHAPTER_WINDOW = 200
_MAX_WINDOWS = 200


class XTruyen(MadaraTemplate):
    base_url = ["https://xtruyen.vn/"]
    language = "vi"
    can_search = True

    # The cover sits on a CDN (img.xtruyen.vn); the og:image points at a local
    # wp-content path that 404s.
    novel_cover_selector = ".summary_image img"
    novel_author_selector = ".author-content"
    novel_synopsis_selector = ".description-summary .summary__content, .description-summary"

    search_item_list_selector = "main.search-main .popular-item-wrap"
    search_item_title_selector = "h5.widget-title a"
    search_item_url_selector = "h5.widget-title a"
    search_item_info_selector = ".list-chapter a.btn-link"

    def initialize(self) -> None:
        super().initialize()
        self.cleaner.bad_css.update(
            [
                ".native-stories",
                ".c-content-readmore",  # "Xem thêm" expand button in the synopsis
            ]
        )

    # ------------------------------------------------------------------------- #
    # Novel information
    # ------------------------------------------------------------------------- #

    def parse_title(self, soup: PageSoup, novel: Novel) -> None:
        # The visible h1 is fully uppercased in the HTML; og:title keeps the
        # proper casing.
        tag = soup.select_one('meta[property="og:title"]')
        novel.title = tag.get("content")
        if not novel.title:
            super().parse_title(soup, novel)

    # ------------------------------------------------------------------------- #
    # Search
    # ------------------------------------------------------------------------- #

    def build_search_url(self, query: str) -> str:
        params = dict(
            s=query,
            post_type="wp-manga",
            m_orderby="keywords",
            page="1",
        )
        return f"{self.scraper.origin}?{urlencode(params)}"

    def parse_search_item_info(self, soup: PageSoup) -> str:
        latest = soup.select_one(self.search_item_info_selector)
        return latest.text.strip() if latest else ""

    # ------------------------------------------------------------------------- #
    # Chapter list
    # ------------------------------------------------------------------------- #

    def parse_chapter_list(
        self,
        tag: PageSoup,
        novel: Novel,
        volume: Optional[Volume] = None,
    ) -> None:
        manga_id = self._parse_manga_id(tag)
        novel_base = self.absolute_url(novel.url).split("?")[0].rstrip("/")

        start = 1
        for _ in range(_MAX_WINDOWS):
            items = self._fetch_chapter_range(manga_id, start, start + _CHAPTER_WINDOW - 1)
            if not items:
                break
            for item in items:
                slug = str(item.get("s") or "").strip()
                if not slug:
                    continue
                title = str(item.get("n") or "").strip()
                extra = str(item.get("e") or "").strip()
                if extra:
                    title = f"{title} : {extra}" if title else extra
                chapter = Chapter(
                    id=len(novel.chapters) + 1,
                    title=title,
                    url=f"{novel_base}/{slug}/",
                )
                if volume is not None:
                    chapter.volume = volume.id
                novel.chapters.append(chapter)
            if len(items) < _CHAPTER_WINDOW:
                break
            start += len(items)

        if not novel.chapters:
            raise LNException("Failed to parse chapter list")

    def _parse_manga_id(self, soup: PageSoup) -> str:
        for script in soup.select("script"):
            match = re.search(r"manga_id[\"']?\s*[:=]\s*[\"']?(\d+)", script.text or "")
            if match:
                return match.group(1)
        raise LNException("No manga id found on novel page")

    def _fetch_chapter_range(self, manga_id: str, start: int, end: int) -> List[dict]:
        url = f"{self.scraper.origin}api/api-chapters.php"
        # The endpoint occasionally answers 500 on the first hit after an idle
        # period; a short retry with backoff keeps whole runs from dying on it.
        last_error: Optional[Exception] = None
        for attempt in range(3):
            if attempt:
                time.sleep(1.5 * attempt)
            try:
                response = self.scraper.post(
                    url,
                    data={
                        "manga_id": manga_id,
                        "from": str(start),
                        "to": str(end),
                        "vol": "",
                    },
                    headers={"X-Custom-Auth": _API_AUTH_HEADER},
                )
                payload = response.json()
                return payload if isinstance(payload, list) else []
            except Exception as e:  # noqa: BLE001 - retried below, raised at the end
                last_error = e
                logger.warning(
                    "Chapter API attempt %d/3 failed for %s (from=%s): %r",
                    attempt + 1,
                    url,
                    start,
                    e,
                )
        raise LNException(f"Chapter API failed after 3 attempts: {last_error!r}") from last_error

    # ------------------------------------------------------------------------- #
    # Chapter body
    # ------------------------------------------------------------------------- #

    def download_chapter(self, chapter: Chapter) -> None:
        url = self.build_chapter_url(chapter)
        soup = self.scraper.get_soup(url)
        body_html = self._decode_chapter_payload(soup)
        if body_html is None:
            # Fall back to plainly rendered content, if any.
            body = soup.select_one(self.chapter_body_selector)
            self.parse_chapter_body(body, chapter)
        else:
            # Unwrap the html/body shell the parser adds around the fragment, so
            # the cleaner sees the chapter's own nodes.
            body = self.scraper.make_soup(body_html).find("body")
            self.parse_chapter_body(body, chapter)

    def _decode_chapter_payload(self, soup: PageSoup) -> Optional[str]:
        for script in soup.select("script"):
            text = script.text or ""
            if "data_x" not in text:
                continue
            match = re.search(r"data_x\s*=\s*[\"']([A-Za-z0-9\-_+/=]+)[\"']", text)
            if not match:
                continue
            encoded = match.group(1).translate(_B64_TRANS)
            encoded += "=" * (-len(encoded) % 4)
            try:
                raw = base64.b64decode(encoded)
                return zlib.decompress(raw).decode("utf-8")
            except Exception:
                logger.debug("Failed to decode chapter payload", exc_info=True)
                return None
        return None
