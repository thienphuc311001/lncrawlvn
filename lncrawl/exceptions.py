from scraper.browser import RenderError, SolveError
from scraper.exceptions import Aborted as AbortedException
from scraper.exceptions import Blocked, Poisoned


class LNException(Exception):
    pass


ScraperErrorGroup = (
    Blocked,
    Poisoned,
    RenderError,
    SolveError,
)

__all__ = [
    "LNException",
    "AbortedException",
    "ScraperErrorGroup",
    "Blocked",
    "Poisoned",
    "RenderError",
    "SolveError",
]
