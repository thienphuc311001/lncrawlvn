# -*- coding: utf-8 -*-
import logging
import re

from lncrawl.core import Chapter, Novel, PageSoup, SoupTemplate

logger = logging.getLogger(__name__)

# In-text page marker repeated at the top of every split chapter page: 第(2/3)页.
_PAGE_MARKER_RE = re.compile(r"第\(\d+/\d+\)页")
# Same marker, capturing the total number of split pages.
_SPLIT_TOTAL_RE = re.compile(r"第\(\d+/(\d+)\)页")
# Empty paragraphs left behind once the marker is stripped from the cleaned HTML.
_EMPTY_PARAGRAPH_RE = re.compile(r"<p[^>]*>\s*</p>")
# Chapter-list pager links: /<bookId>/index_5.html. index_99999.html is the
# jump-to-page widget, not a real page.
_PAGER_PAGE_RE = re.compile(r"index_(\d+)\.html")


class SeventeenKNovel(SoupTemplate):
    """Crawler for 17k小说网 (www.17ktxt.com) — Chinese web novels.

    Two things make this site different from a stock SoupTemplate fit:

    1. The chapter list is paginated at /<bookId>/index_<n>.html, ~100 rows per
       page, fully server-rendered in oldest-first order — no browser needed.
       Each index page also carries a newest-first "latest 12" teaser panel
       under `.book_list`, so the real list must be scoped to `.book_list2`
       (the site's own JS merely reverses it for display). The page count comes
       from the pager's highest index link.
    2. Long chapters are split across <id>.html / <id>_2.html / ... pages. The
       split count is printed in the body as 第(n/m)页 and every split page
       repeats the marker; the markers are stripped from the cleaned output.
    """

    base_url = ["https://www.17ktxt.com/"]
    language = "zh"
    request_rate_limit = 1.0

    novel_title_selector = "h1"
    novel_cover_selector = "meta[property='og:image']"
    novel_author_selector = "meta[property='og:novel:author']"
    chapter_body_selector = "article.font_max"

    def parse_authors(self, soup: PageSoup, novel: Novel) -> None:
        # og:novel:author only exists in the namespaced form, which the default
        # fallback never matches, so read the content attribute here.
        tag = soup.select_one(self.novel_author_selector)
        if tag:
            novel.author = str(tag.get("content") or "").strip()

    def parse_tags(self, soup: PageSoup, novel: Novel) -> None:
        # The keywords meta holds SEO junk; the category meta is the real genre.
        tag = soup.select_one("meta[property='og:novel:category']")
        if tag:
            novel.tags = [t.strip() for t in str(tag.get("content") or "").split(",") if t.strip()]

    def parse_summary(self, soup: PageSoup, novel: Novel) -> None:
        tag = soup.select_one("meta[property='og:description']")
        if tag:
            # og:description carries literal \n escape sequences in the source.
            novel.synopsis = str(tag.get("content") or "").replace("\\n", "\n").strip()

    def build_novel_url(self, novel: Novel) -> str:
        # absolute_url strips the trailing slash, and this site 404s on the
        # bare book path — the directory form is required.
        return f"{self.absolute_url(novel.url).rstrip('/')}/"

    def parse_toc(self, soup: PageSoup, novel: Novel) -> None:
        base = self.build_novel_url(novel)
        seen: set[str] = set()
        total_pages = 1
        page_number = 1
        while page_number <= total_pages:
            page_soup = self.scraper.get_soup(f"{base}index_{page_number}.html")
            if page_number == 1:
                total_pages = self._list_page_count(page_soup)
            for tag in page_soup.select("div.book_list2 ul li a"):
                href = str(tag.get("href") or "")
                if not href or href in seen:
                    continue
                seen.add(href)
                novel.add_chapter(title=tag.text.strip(), url=self.absolute_url(href))
            page_number += 1
        if not seen:
            raise Exception(f"No chapters found under {base}")

    def download_chapter(self, chapter: Chapter) -> None:
        url = self.absolute_url(chapter.url)
        soup = self.scraper.get_soup(url)
        body = soup.select_one(self.chapter_body_selector)
        if not body:
            raise Exception(f"Chapter body not found at {url}")
        parts: list[str] = [body.inner_html]
        prefix = re.sub(r"\.html$", "", url)
        for n in range(2, self._split_page_count(body) + 1):
            page_soup = self.scraper.get_soup(f"{prefix}_{n}.html")
            page_body = page_soup.select_one(self.chapter_body_selector)
            if page_body:
                parts.append(page_body.inner_html)
        combined = PageSoup.create(f"<article>{''.join(parts)}</article>")
        self.parse_chapter_body(combined.select_one("article"), chapter)

    def parse_chapter_body(self, soup: PageSoup, chapter: Chapter) -> None:
        super().parse_chapter_body(soup, chapter)
        if chapter.body:
            body = _PAGE_MARKER_RE.sub("", chapter.body)
            chapter.body = _EMPTY_PARAGRAPH_RE.sub("", body)

    def _list_page_count(self, page_soup: PageSoup) -> int:
        """Read the chapter-list pager and return its highest real page number."""
        total = 1
        for tag in page_soup.select(".pagination a[href*='index_']"):
            match = _PAGER_PAGE_RE.search(str(tag.get("href") or ""))
            page = int(match.group(1)) if match else 0
            if 0 < page < 1000:
                total = max(total, page)
        return total

    def _split_page_count(self, body: PageSoup) -> int:
        """Read 第(n/m)页 from the chapter body and return m (1 when missing)."""
        match = _SPLIT_TOTAL_RE.search(body.get_text(" ", strip=True))
        return int(match.group(1)) if match else 1
