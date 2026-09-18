"""Minimal application context for the console tool.

The full Lightnovel Crawler exposes a large service graph (DB, users, jobs,
server). This tool needs only three things: a logger, one shared scraper state,
and the list of bundled source crawlers. Everything else was dropped.
"""

import logging
import os
import sys
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# Load server configuration from the project root, regardless of the launch directory.
# Deployment environment variables take precedence over values in this file.
load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)

logger = logging.getLogger("lncrawl")

APP_DIR = Path(os.getenv("LNCRAWL_DATA_PATH") or Path.home() / ".lncrawl-mini").absolute()


class LoggerProxy:
    """Small shim standing in for the server Logger service."""

    def __init__(self, level: int = logging.WARNING):
        self._level = level
        self._handlers: list = []
        self.progress_bar: bool = False

    @property
    def level(self) -> int:
        return self._level

    @property
    def is_debug(self) -> bool:
        return self._level == logging.DEBUG

    @property
    def is_info(self) -> bool:
        return self._level <= logging.INFO

    def setup(self, level: Optional[int] = None) -> None:
        if level is None:
            level = logging.INFO
        self._level = int(level)
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(self._level)
        self.progress_bar = not self.is_info

    def log(self, level: int, *args, **kwargs):
        stacklevel = kwargs.pop("stacklevel", 2)
        logger.log(level, *args, stacklevel=stacklevel + 1, **kwargs)

    def error(self, *args, **kwargs):
        self.log(logging.ERROR, *args, **kwargs)

    def warn(self, *args, **kwargs):
        self.log(logging.WARNING, *args, **kwargs)

    def info(self, *args, **kwargs):
        self.log(logging.INFO, *args, **kwargs)

    def debug(self, *args, **kwargs):
        self.log(logging.DEBUG, *args, **kwargs)


class SimpleConfig:
    """The handful of settings the scraper layer reads, as plain attributes."""

    max_sessions_per_exit = 2
    max_attempts = 5
    max_rotations = 2
    solve_timeout = 90.0
    use_archive = False
    archive_max_age = 0.0
    impersonate = ""
    ignore_images = False
    # VietPhrase-safe export chunking (``None`` = unset, so the environment
    # variable stays visible to the binder; an explicit value always wins).
    safe_block_target = None
    safe_block_max = None


class AppContext:
    def __init__(self) -> None:
        self.logger = LoggerProxy()
        self.config = SimpleConfig()

    def setup(self, log_level: Optional[int] = None) -> None:
        self.logger.setup(log_level)
        from .services.scraper import ctx_scraper

        ctx_scraper.data_dir = APP_DIR / "scraper"

    def destroy(self) -> None:
        from .services.scraper import ctx_scraper

        ctx_scraper.close()


ctx = AppContext()
