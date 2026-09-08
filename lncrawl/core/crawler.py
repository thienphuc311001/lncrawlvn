from __future__ import annotations

import hashlib
import math
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Union

from scraper import extract_base

from ..exceptions import LNException
from ..utils.file_tools import atomic_write
from ..utils.text_tools import format_title, normalize
from .models import Chapter, Novel, Volume
from .tiers import LEGACY

if TYPE_CHECKING:
    from requests import Response
    from scraper import Diagnosis, Scraper


class Crawler(ABC):
    base_url: Union[str, List[str]]

    language = ""
    has_mtl = False
    has_manga = False

    can_login = False
    can_search = False

    request_rate_limit: float = 3.0

    chapters_per_volume = 100
    auto_generate_cover = True

    is_disabled = False
    disable_reason = ""
    version = 0

    # Worker count for this process's task manager; a small fixed value keeps
    # the tool concurrent without hammering any single site.
    max_sessions_per_exit = 2

    tier = LEGACY

    # The novel being crawled, set before any stage runs.
    novel_url: str = ""

    @classmethod
    def max_workers(cls) -> int:
        return max(1, cls.max_sessions_per_exit + 1)

    def __init__(
        self,
        parser: Optional[str] = None,
        origin: Optional[str] = None,
        *,
        scraper: Optional["Scraper"] = None,
    ) -> None:
        """Create a standalone Crawler instance.

        Args:
            parser: Desired parser ("lxml", "html.parser", ...).
            origin: Origin URL of the source.
            scraper: The session to crawl with. Supplied by the caller normally.
        """
        if isinstance(self.base_url, str):
            self.base_url = [self.base_url]
        if not origin or origin not in self.base_url:
            origin = self.base_url[0]

        from ..services.scraper import ctx_scraper

        self.scraper = scraper or ctx_scraper.open(
            origin,
            parser=parser,
            rate_limit=self.request_rate_limit,
        )
        self.scraper.check_response = self.check_response

        from .cleaner import TextCleaner
        from .taskman import TaskManager

        self.cleaner = TextCleaner()
        self.taskman = TaskManager(workers=self.max_workers())

    @property
    def parser(self) -> str:
        return self.scraper.parser

    @parser.setter
    def parser(self, value: str) -> None:
        self.scraper.parser = value

    def close(self) -> None:
        try:
            self.scraper.close()
        except Exception:
            pass
        self.taskman.shutdown()

    def download_image(self, url: str, output_file: Path) -> None:
        """Download an image from the source and save it to the target file."""
        if not url:
            raise LNException("URL is missing")

        if output_file.is_file():
            os.utime(output_file)
            return

        img = self.scraper.get_image(url)
        if img.mode not in ("L", "RGB", "YCbCr", "RGBX"):
            if img.mode == "RGBa":
                img = img.convert("RGBA").convert("RGB")
            else:
                img = img.convert("RGB")

        with atomic_write(output_file) as tmp:
            img.save(tmp, "JPEG", optimized=True)

    def download_cover(self, cover_url: str, cover_file: Path) -> None:
        from ..context import ctx

        try:
            self.download_image(cover_url, cover_file)
            ctx.logger.debug(f"Cover saved: {cover_url} -> {cover_file}")
            return
        except Exception as e:
            ctx.logger.warn(
                f"Cover download failed: {cover_url} -> {cover_file}",
                exc_info=ctx.logger.is_debug,
            )
            if not self.auto_generate_cover:
                raise LNException("Failed to download cover") from e

        if cover_file.is_file():
            os.utime(cover_file)
            return

        from ..utils import imgen

        try:
            img = imgen.generate_cover_image()
            with atomic_write(cover_file) as tmp:
                img.save(tmp, "JPEG", optimized=True)
            ctx.logger.debug(f"Cover generated: {cover_file}")
        except Exception:
            ctx.logger.warn(
                f"Could not generate a cover at {cover_file}",
                exc_info=ctx.logger.is_debug,
            )

    def format_novel(self, novel: Novel) -> None:
        if not novel.title:
            raise LNException("Novel title is missing")

        novel.title = format_title(novel.title)
        novel.author = ", ".join(filter(None, map(format_title, novel.author.split(","))))
        novel.tags = list(filter(None, map(normalize, set(novel.tags or []))))

        total_volumes = len(novel.volumes)
        total_chapters = len(novel.chapters)

        novel.volumes.sort(key=lambda x: x.id)
        novel.chapters.sort(key=lambda x: x.id)

        if total_volumes == 0 and total_chapters > 0:
            total_volumes = math.ceil(total_chapters / self.chapters_per_volume)
            novel.volumes = [Volume(id=i + 1) for i in range(total_volumes)]

        vol_id_map: Dict[int, int] = {}
        for index, volume in enumerate(novel.volumes):
            vol_id_map[volume.id] = index
            volume.id = index + 1
            volume.chapters = 0
            volume.title = format_title(volume.title) or f"Volume {volume.id}"

        unknown_volume = Volume(id=total_volumes + 1, title="Unknown", chapters=0)

        for index, chapter in enumerate(novel.chapters):
            chapter.id = index + 1
            chapter.url = str(chapter.url)
            chapter.title = format_title(chapter.title) or f"Chapter {chapter.id}"

            if chapter.volume not in vol_id_map:
                chapter.volume = 1 + (index // self.chapters_per_volume)

            vol_index = vol_id_map.get(chapter.volume, -1)
            if vol_index == -1 and novel.volumes[-1] != unknown_volume:
                novel.volumes.append(unknown_volume)

            volume = novel.volumes[vol_index]
            chapter.volume = volume.id
            volume.chapters += 1

    def format_chapter(self, chapter: Chapter) -> None:
        if not chapter.title:
            raise LNException("Chapter title is missing")

        chapter.title = format_title(chapter.title)
        chapter.body = (chapter.body or "").strip()
        chapter.success = bool(chapter.body)
        self._extract_images(chapter)

    def _extract_images(self, chapter: Chapter) -> None:
        chapter.setdefault("images", {})
        if not chapter.body:
            return

        soup = self.scraper.make_soup(chapter.body)
        for img in soup.select("img[src]"):
            src_url = img.get_attr("src")
            if not src_url:
                continue

            full_url = self.absolute_url(src_url, page_url=chapter.url)
            if not full_url.startswith("http"):
                continue

            id_text = str([chapter.url, full_url])
            image_id = hashlib.md5(id_text.encode()).hexdigest()
            img.attrs = {"src": f"images/{image_id}.jpg", "alt": image_id}
            chapter.images[image_id] = full_url

        if chapter.images:
            chapter.body = soup.body.inner_html
        soup.decompose()

    # ------------------------------------------------------------------
    # Methods to implement in crawler
    # ------------------------------------------------------------------

    def initialize(self) -> None:
        pass

    def check_response(self, response: "Response", body: str) -> Optional["Diagnosis"]:
        return None

    def login(self, email: str, password_or_token: str) -> None:
        pass

    @abstractmethod
    def read_novel(self, novel: Novel) -> None:
        """Scrape the novel details from the source using novel.url"""
        raise NotImplementedError()

    @abstractmethod
    def download_chapter(self, chapter: Chapter) -> None:
        """Download the chapter and set body on the chapter object."""
        raise NotImplementedError()

    # ------------------------------------------------------------------
    # Utility methods that can be overridden
    # ------------------------------------------------------------------

    def absolute_url(self, any_url: Any, page_url: Optional[str] = None) -> str:
        url = str(any_url or "").strip().rstrip("/")
        if not url:
            return url

        scheme = url.split(":")[0]
        if scheme in ("http", "https"):
            return url

        base_url = extract_base(self.scraper.last_url).strip("/")
        if url.startswith("//"):
            scheme = base_url.split(":")[0]
            return f"{scheme}:{url}"

        if url.startswith("/"):
            return base_url + url

        if not page_url:
            page_url = self.scraper.last_url
        page_url = page_url.rstrip("/")

        if url.startswith("."):
            paths = page_url.split("/")
            parts = url.split("/")
            while parts and (parts[0] == ".." or parts[0] == "."):
                parts = parts[1:]
                if parts and parts[0] == "..":
                    paths = paths[:-1]
                    parts = parts[1:]
            return "/".join(paths + parts)

        return f"{page_url}/{url}"
