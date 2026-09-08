"""Crawl engine: base classes, templates and shared models.

Sources do ``from lncrawl.core import ...`` and the names below are what the
bundled crawlers actually use, exposed directly (no lazy loading needed for a
single-process CLI).
"""

from scraper import PageSoup, Scraper

from .cleaner import TextCleaner
from .crawler import Crawler
from .legacy import LegacyCrawler
from .models import Chapter, Novel, SearchResult, Volume
from .taskman import TaskManager
from .template import CrawlerTemplate, SoupTemplate
from .tiers import LEGACY

__all__ = [
    "Crawler",
    "PageSoup",
    "Scraper",
    "TaskManager",
    "TextCleaner",
    "Novel",
    "Volume",
    "Chapter",
    "SearchResult",
    "CrawlerTemplate",
    "LegacyCrawler",
    "SoupTemplate",
    "LEGACY",
]
