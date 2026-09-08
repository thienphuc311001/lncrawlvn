"""Source registry: discovers the bundled crawlers and pick one by URL.

Only ``sources/zh`` and ``sources/vi`` are scanned — that is the whole point of
this tool. Crawlers are imported from the top-level ``sources`` package, keeping
the ``from sources.zh.xxx import ...`` cross-imports used by a few files intact.
"""

import inspect
import logging
from importlib import import_module
from pathlib import Path
from typing import Dict, Type

from scraper import validate_url

from ..core import Crawler
from ..exceptions import LNException
from ..services.scraper import ctx_scraper

logger = logging.getLogger(__name__)

_SOURCES_DIR = Path(__file__).resolve().parent.parent.parent / "sources"


def _iter_crawler_files():
    for lang_dir in ("zh", "vi"):
        folder = _SOURCES_DIR / lang_dir
        if not folder.is_dir():
            continue
        for file in sorted(folder.glob("*.py")):
            if file.name.startswith("_") or not file.name[0].isalnum():
                continue
            yield lang_dir, file


class Sources:
    def __init__(self) -> None:
        self.crawlers: Dict[str, Type[Crawler]] = {}
        self._loaded = False

    def load(self) -> None:
        if self._loaded:
            return

        for lang_dir, file in _iter_crawler_files():
            module_path = f"sources.{lang_dir}.{file.stem}"
            try:
                module = import_module(module_path)
            except Exception as e:
                logger.warning(f"[{file}] Failed to load: {e!r}")
                continue

            for name in dir(module):
                cls = getattr(module, name)
                if (
                    not inspect.isclass(cls)
                    or not issubclass(cls, Crawler)
                    or cls is Crawler
                    or cls.__dict__.get("is_template")
                    or getattr(cls, "__module__", "") != module.__name__
                ):
                    continue
                if inspect.isabstract(cls):
                    continue

                base_url = getattr(cls, "base_url", [])
                urls = [base_url] if isinstance(base_url, str) else base_url
                for url in urls:
                    url = str(url).lower().strip("/") + "/"
                    if not validate_url(url):
                        continue
                    self.crawlers[url] = cls

        self._loaded = True
        logger.debug(f"Loaded {len(self.crawlers)} source URLs")

    def init_crawler(self, url: str) -> Crawler:
        self.load()
        if not url:
            raise LNException("Empty URL")
        url = url.lower().strip("/") + "/"
        candidates = [u for u in self.crawlers if url.startswith(u)]
        if not candidates:
            raise LNException(f"No crawler for the host of {url}")
        base_url = sorted(candidates, key=len, reverse=True)[0]
        crawler_cls = self.crawlers[base_url]
        logger.debug(f"Using {crawler_cls} to crawl {url}")
        crawler = crawler_cls(
            origin=base_url,
            scraper=ctx_scraper.open(
                base_url,
                rate_limit=getattr(crawler_cls, "request_rate_limit", 3.0),
            ),
        )
        crawler.initialize()
        return crawler
