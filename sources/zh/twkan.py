# -*- coding: utf-8 -*-
import logging

from lncrawl.core import Chapter, Novel, PageSoup, SoupTemplate

logger = logging.getLogger(__name__)


class TwkanCrawler(SoupTemplate):
    base_url = "https://twkan.com/"
    language = "zh"
    request_rate_limit = 20

    novel_title_selector = "h1"
    chapter_body_selector = "div#txtcontent0"

    def parse_toc(self, soup: PageSoup, novel: Novel) -> None:
        """Parse table of contents from the catalog page."""
        # Extract the book ID from the novel URL
        # Example: https://twkan.com/book/102773.html -> 102773
        import re

        match = re.search(r"/book/(\d+)", novel.url)
        if not match:
            logger.error(f"Could not extract book ID from URL: {novel.url}")
            return

        book_id = match.group(1)
        # Call the AJAX endpoint that loads all chapters
        catalog_url = f"https://twkan.com/ajax_novels/chapterlist/{book_id}.html"
        catalog = self.scraper.get_soup(catalog_url)

        for a in catalog.select("ul li a"):
            title = a.text.strip()
            url = self.absolute_url(a.get("href", ""))
            if title and url:
                novel.add_chapter(title=title, url=url)

    def download_chapter(self, chapter: Chapter) -> None:
        """Download chapter content."""
        soup = self.scraper.get_soup(self.build_chapter_url(chapter))
        body = soup.select_one(self.chapter_body_selector)

        if not body:
            logger.warning(f"Chapter body not found for {chapter.url}")
            return

        self.parse_chapter_body(body, chapter)
