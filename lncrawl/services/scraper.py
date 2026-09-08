"""The only place a ``Scraper`` is constructed.

Everything keyed by origin — pacing, identity, what has been learned — lives in
one process-wide ``SharedState``, shared by every crawler session this process
opens. The external ``scraper`` package owns the anti-bot machinery; this class
only wires the glue and exposes ``ctx_scraper`` for the core to open sessions.
"""

from __future__ import annotations

import logging
import threading
from importlib import util
from typing import TYPE_CHECKING, Any, Dict, Optional

from scraper import BROWSER_MODES, pick_chromium, pick_firefox

from ..context import ctx

if TYPE_CHECKING:
    from scraper import Scraper, ScraperConfig, SharedState

logger = logging.getLogger(__name__)


class ScraperService:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._state: Optional["SharedState"] = None
        self._plain: Optional["Scraper"] = None
        self._solver = None
        self._solver_ready = False
        self.data_dir = None

    def _build_solver(self):
        if not util.find_spec("websockets"):
            return None

        wanted = (getattr(ctx.config, "browser_mode", "") or "").strip().lower()
        if wanted not in BROWSER_MODES:
            wanted = "auto"

        firefox = pick_firefox()
        if firefox:
            from scraper import BidiSolver

            return BidiSolver(executable=firefox, mode=wanted)

        chromium = pick_chromium()
        if chromium:
            from scraper import CdpSolver

            return CdpSolver(executable=chromium, mode=wanted)
        return None

    @property
    def solver(self):
        with self._lock:
            if not self._solver_ready:
                self._solver = self._build_solver()
                self._solver_ready = True
            return self._solver

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def _crawl_config(
        self,
        *,
        parser: Optional[str] = None,
        warmup: bool = True,
        raise_for_status: bool = True,
        timeout: Optional[float] = None,
        probe: bool = False,
    ) -> "ScraperConfig":
        from scraper import PacingPolicy, ScraperConfig

        crawler = ctx.config
        settings: Dict[str, Any] = {
            "exits": [],
            "data_dir": self.data_dir,
            "browser": None if probe else self.solver,
            "guard_topic": False,
            "max_sessions_per_exit": crawler.max_sessions_per_exit,
            "max_attempts": crawler.max_attempts,
            "max_rotations": crawler.max_rotations,
            "solve_timeout": crawler.solve_timeout,
            "archive": crawler.use_archive,
            "archive_max_age": crawler.archive_max_age,
            "raise_for_status": raise_for_status,
            "pacing": PacingPolicy(warmup=warmup),
        }
        if parser:
            settings["parser"] = parser
        if timeout:
            settings["timeout"] = (timeout, timeout)
        if crawler.impersonate:
            settings["impersonate"] = crawler.impersonate
        return ScraperConfig(**settings)

    def _plain_config(self) -> "ScraperConfig":
        from scraper import PacingPolicy, ScraperConfig

        return ScraperConfig(
            remember=False,
            guard_topic=False,
            browser=None,
            pacing=PacingPolicy(interval=0.0, floor=0.0, warmup=False),
        )

    # ------------------------------------------------------------------
    # Shared state
    # ------------------------------------------------------------------

    @property
    def state(self) -> "SharedState":
        from scraper import SharedState

        with self._lock:
            if self._state is None:
                self._state = SharedState.create(self._crawl_config())
            return self._state

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------

    def open(
        self,
        origin: Optional[str] = None,
        *,
        parser: Optional[str] = None,
        rate_limit: float = 0.0,
        warmup: bool = True,
        raise_for_status: bool = True,
        timeout: Optional[float] = None,
    ) -> "Scraper":
        from scraper import Scraper

        scraper = Scraper(
            origin=origin or "",
            parser=parser,
            config=self._crawl_config(
                parser=parser,
                warmup=warmup,
                raise_for_status=raise_for_status,
                timeout=timeout,
            ),
            state=self.state,
        )
        if origin and rate_limit > 0:
            self.state.pacer.learn(self.state.memory.key(origin), 1.0 / rate_limit)
        return scraper

    def plain(self) -> "Scraper":
        from scraper import Scraper

        with self._lock:
            if self._plain is None:
                self._plain = Scraper(config=self._plain_config())
            return self._plain

    def set_rate_limit(self, url: str, requests_per_second: float) -> None:
        if requests_per_second <= 0:
            return
        interval = 1.0 / requests_per_second
        state = self.state
        state.memory.profile(url).interval = interval
        state.memory.touch()
        state.pacer.learn(state.memory.key(url), interval)

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            plain, self._plain = self._plain, None
            state, self._state = self._state, None
            solver, self._solver = self._solver, None
            self._solver_ready = False
        if plain is not None:
            try:
                plain.close()
            except Exception:
                pass
        if state is not None:
            try:
                state.exits.release_all()
            except Exception:
                pass
        if solver is not None:
            try:
                solver.close()
            except Exception:
                pass


ctx_scraper = ScraperService()
