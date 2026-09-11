"""FastAPI server exposing the crawl pipeline over HTTP.

Development wiring: ``lnmini run dev`` starts this alongside the Next.js
frontend (see ``scripts/lnmini.sh``). The CLI in ``lncrawl.app`` remains the
primary interface.

``POST /api/extract`` starts a crawl job in a background thread and returns a
job id immediately. ``GET /api/jobs/{job_id}`` reports progress: a timestamped
log of every stage, plus one status entry per chapter (success / failure and
the failure reason).
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .context import APP_DIR, ctx
from .core import Novel
from .exceptions import LNException
from .library import LIBRARY

logger = logging.getLogger(__name__)

_MAX_JOBS = 50
_MAX_LOG_LINES = 1000
_SETTINGS_PATH = APP_DIR / "settings.json"


class JobLog(BaseModel):
    t: float = Field(..., description="Seconds since the job started.")
    level: str = Field("info", description="info | warning | error")
    message: str


class JobChapter(BaseModel):
    id: int
    title: str = ""
    url: str = ""
    success: bool = False
    error: Optional[str] = None


class BookChapter(BaseModel):
    id: int
    title: str = ""
    url: str = ""
    saved: bool = False


class BookSummary(BaseModel):
    book_id: str
    title: str
    author: str = ""
    cover_url: str = ""
    total_chapters: int = 0
    saved_count: int = 0
    saved_at: float = 0.0


class BookDetail(BookSummary):
    url: str = ""
    language: Optional[str] = None
    synopsis: str = ""
    tags: List[str] = []
    chapters: List[BookChapter] = []


class ChapterContent(BaseModel):
    id: int
    title: str = ""
    url: str = ""
    body: str = ""
    fetched_at: float = 0.0


class Job(BaseModel):
    job_id: str
    url: str
    status: str = "pending"  # pending | running | done | failed
    source: str = ""  # crawler class that handled the URL
    title: str = ""
    author: str = ""
    cover_url: str = ""
    language: Optional[str] = None
    synopsis: str = ""
    tags: List[str] = []
    total_chapters: int = 0
    requested: str = "all"
    book_id: str = ""
    saved_count: int = 0
    success_count: int = 0
    failed_count: int = 0
    error: Optional[str] = None
    started_at: float = 0.0
    finished_at: Optional[float] = None
    first_log_index: int = Field(
        0, description="Global index of logs[0]; grows when old lines are evicted."
    )
    logs: List[JobLog] = []
    chapters: List[JobChapter] = []


class JobStore:
    """Thread-safe in-memory job registry; oldest jobs are evicted."""

    def __init__(self, max_jobs: int = _MAX_JOBS) -> None:
        self.lock = threading.RLock()
        self._jobs: Dict[str, Job] = {}
        self._order: Deque[str] = deque()
        self._max_jobs = max_jobs

    def create(self, url: str) -> Job:
        job = Job(job_id=uuid.uuid4().hex[:12], url=url, started_at=time.time())
        with self.lock:
            self._jobs[job.job_id] = job
            self._order.append(job.job_id)
            while len(self._order) > self._max_jobs:
                self._jobs.pop(self._order.popleft(), None)
        return job

    def snapshot(self, job_id: str) -> Optional[Job]:
        with self.lock:
            job = self._jobs.get(job_id)
            return job.model_copy(deep=True) if job else None

    def get_ref(self, job_id: str) -> Optional[Job]:
        """Return the live job object; mutate it only under ``JOBS.lock``."""
        with self.lock:
            return self._jobs.get(job_id)

    def has_active_for_book(self, book_id: str) -> bool:
        """True while any pending/running job is writing to this book."""
        with self.lock:
            return any(
                job.book_id == book_id and job.status in ("pending", "running")
                for job in self._jobs.values()
            )


JOBS = JobStore()


def _log(job: Job, message: str, level: str = "info") -> None:
    with JOBS.lock:
        job.logs.append(
            JobLog(t=round(time.time() - job.started_at, 2), level=level, message=message)
        )
        overflow = len(job.logs) - _MAX_LOG_LINES
        if overflow > 0:
            del job.logs[:overflow]
            job.first_log_index += overflow


def _set(job: Job, **fields) -> None:
    with JOBS.lock:
        for key, value in fields.items():
            setattr(job, key, value)


# --------------------------------------------------------------------------- #
# Crawler log forwarding
# --------------------------------------------------------------------------- #
# The job console shows what the server itself logs through ``_log``; what the
# crawler reports (TOC-walk heartbeats and ETAs, blocked-window waits, skipped
# ids) stays invisible unless it is routed into the job log too. The crawlers
# log through Python's ``logging`` on module loggers under ``sources.*``, so
# one process-wide handler forwards those lines into the job running on the
# current thread, falling back to the most recent job for the download worker
# threads (which have no thread-local of their own).

_ACTIVE_JOB = threading.local()
_LATEST_JOB: Optional[Job] = None
_LATEST_JOB_LOCK = threading.Lock()


class _CrawlerLogHandler(logging.Handler):
    """Forward INFO+ records from the crawlers and the scraper into a job log."""

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self._prefixes = ("sources.", "scraper.")

    def emit(self, record: logging.LogRecord) -> None:
        if not record.name.startswith(self._prefixes):
            return
        job = getattr(_ACTIVE_JOB, "job", None)
        if job is None:
            job = _LATEST_JOB
        if job is None:
            return
        level = {logging.WARNING: "warning", logging.ERROR: "error"}.get(
            record.levelno, "info"
        )
        try:
            _log(job, record.getMessage(), level=level)
        except Exception:  # noqa: BLE001 - a logging side path must not fail a crawl
            pass


_LOGGING_HANDLER = _CrawlerLogHandler()


def _install_log_forwarding(job: Job) -> None:
    global _LATEST_JOB
    _ACTIVE_JOB.job = job
    with _LATEST_JOB_LOCK:
        _LATEST_JOB = job
        if _LOGGING_HANDLER not in logging.getLogger().handlers:
            logging.getLogger().addHandler(_LOGGING_HANDLER)
    # The app context opens the loggers at WARNING; let the crawlers' INFO
    # lines through so the heartbeats and ETAs reach the console.
    logging.getLogger("sources").setLevel(logging.INFO)


def _uninstall_log_forwarding() -> None:
    # The fallback must not outlive the job: without this, worker threads of
    # a later crawl land their lines in this finished job's console, and any
    # stray INFO from the scraper keeps appending to it after completion.
    global _LATEST_JOB
    _ACTIVE_JOB.job = None
    with _LATEST_JOB_LOCK:
        _LATEST_JOB = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    ctx.setup(log_level=logging.WARNING)
    _apply_settings(_load_settings())
    logger.info("lncrawl-mini API ready (data dir: %s)", APP_DIR)
    yield
    from .services.scraper import ctx_scraper

    ctx_scraper.close()


app = FastAPI(
    title="lncrawl-mini",
    description="Minimal web-novel extraction API with live crawl job progress.",
    version="0.2.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ExtractRequest(BaseModel):
    url: str = Field(..., description="Novel details page URL.")
    first: Optional[int] = Field(None, ge=1, description="Only the first N chapters.")
    last: Optional[int] = Field(None, ge=1, description="Only the last N chapters.")
    rate_limit: Optional[float] = Field(None, gt=0, description="Requests per second cap.")
    workers: Optional[int] = Field(None, ge=1, le=16, description="Concurrent chapter workers.")
    save: bool = Field(True, description="Persist the novel and fetched chapters into the library.")
    overwrite: bool = Field(
        False,
        description="Re-crawled chapters replace already-saved copies (repairs truncated bodies).",
    )
    only_ids: Optional[List[int]] = Field(
        None, description="Internal: download only these chapter ids (fetch-missing runs)."
    )
    sync: bool = Field(False, description="Run inline and return the final job (old behavior).")


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/api/extract", response_model=Job, status_code=202)
def extract(req: ExtractRequest) -> Job:
    """Start a crawl job. Returns 202 with the job; poll /api/jobs/{id}.

    With ``sync: true`` the crawl runs inline and the finished job is returned
    directly (the pre-0.2 behavior).
    """
    job = JOBS.create(req.url)
    if req.sync:
        _run_job(job.job_id, req)
        final = JOBS.snapshot(job.job_id)
        assert final is not None
        return final
    thread = threading.Thread(
        target=_run_job,
        args=(job.job_id, req),
        daemon=True,
        name=f"crawl-{job.job_id}",
    )
    thread.start()
    started = JOBS.snapshot(job.job_id)
    assert started is not None
    return started


@app.get("/api/jobs/{job_id}", response_model=Job)
def get_job(job_id: str) -> Job:
    job = JOBS.snapshot(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.get("/api/books", response_model=List[BookSummary])
def list_books() -> List[BookSummary]:
    """All novels persisted in the library, newest first."""
    return [BookSummary(**b) for b in LIBRARY.list_books()]


@app.get("/api/books/{book_id}", response_model=BookDetail)
def get_book(book_id: str) -> BookDetail:
    """Book metadata plus its table of contents with saved/missing flags."""
    book = LIBRARY.load_book(book_id)
    if book is None:
        raise HTTPException(status_code=404, detail="Book not found in library")
    return BookDetail(**book)


@app.get("/api/books/{book_id}/cover")
def get_cover(book_id: str):
    cover = LIBRARY.cover_path(book_id)
    if not cover.is_file():
        raise HTTPException(status_code=404, detail="No cover saved")
    return FileResponse(cover, media_type="image/jpeg")


@app.get("/api/books/{book_id}/chapters/{chapter_id}", response_model=ChapterContent)
def get_chapter(book_id: str, chapter_id: int) -> ChapterContent:
    data = LIBRARY.load_chapter(book_id, chapter_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Chapter not saved yet")
    return ChapterContent(**data)


@app.delete("/api/books/{book_id}/chapters/{chapter_id}", status_code=204)
def delete_chapter(book_id: str, chapter_id: int):
    """Remove one saved chapter file so it can be re-fetched from the source.

    The TOC entry in ``book.json`` is kept, so a ``fetch missing`` run can
    download the chapter again later.
    """
    if JOBS.has_active_for_book(book_id):
        raise HTTPException(
            status_code=409,
            detail="A crawl job is still running for this book — stop it first",
        )
    if LIBRARY.load_book(book_id) is None:
        raise HTTPException(status_code=404, detail="Book not found in library")
    try:
        deleted = LIBRARY.delete_chapter(book_id, chapter_id)
    except LNException as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not deleted:
        raise HTTPException(status_code=404, detail="Chapter not saved yet")


@app.post("/api/books/{book_id}/fetch-missing", response_model=Job, status_code=202)
def fetch_missing(book_id: str) -> Job:
    """Start a job that downloads only chapters missing from the library."""
    book = LIBRARY.load_book(book_id)
    if book is None:
        raise HTTPException(status_code=404, detail="Book not found in library")
    if not book["url"]:
        raise HTTPException(status_code=400, detail="Book has no source URL")
    missing = LIBRARY.missing_chapter_ids(book_id)
    if not missing:
        raise HTTPException(status_code=400, detail="No missing chapters — the book is complete")
    req = ExtractRequest(url=book["url"], only_ids=missing)
    job = JOBS.create(book["url"])
    thread = threading.Thread(
        target=_run_job,
        args=(job.job_id, req),
        daemon=True,
        name=f"crawl-{job.job_id}",
    )
    thread.start()
    started = JOBS.snapshot(job.job_id)
    assert started is not None
    return started


@app.get("/api/books/{book_id}/export")
def export_book(book_id: str, format: str = Query("epub", pattern="^(epub|txt)$")):
    """Export saved chapters as an EPUB or TXT wrapped in a ZIP download."""
    try:
        zip_path = LIBRARY.export_zip(book_id, format)
    except LNException as e:
        raise HTTPException(status_code=400, detail=str(e))
    return FileResponse(zip_path, media_type="application/zip", filename=zip_path.name)


@app.delete("/api/books/{book_id}", status_code=204)
def delete_book(book_id: str):
    """Delete a book with all its saved chapters, cover, and exports."""
    if JOBS.has_active_for_book(book_id):
        raise HTTPException(
            status_code=409,
            detail="A crawl job is still running for this book — stop it first",
        )
    try:
        deleted = LIBRARY.delete_book(book_id)
    except LNException as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not deleted:
        raise HTTPException(status_code=404, detail="Book not found in library")


class ConfigField(BaseModel):
    key: str
    type: str = "text"  # int | float | bool | text
    label: str = ""
    help: str = ""
    value: Any = None
    default: Any = None
    min: Optional[float] = None
    max: Optional[float] = None
    step: Optional[float] = None


def _settings_path() -> Path:
    return APP_DIR / "settings.json"


def _load_settings() -> Dict[str, Any]:
    path = _settings_path()
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _persist_settings(values: Dict[str, Any]) -> None:
    try:
        _settings_path().write_text(
            json.dumps(values, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    except Exception as e:
        logger.warning("Could not persist settings: %s", e)


def _apply_settings(values: Dict[str, Any]) -> None:
    fields = {f.key: f for f in CONFIG_FIELDS}
    for key, value in values.items():
        field = fields.get(key)
        if field is None:
            continue
        if field.type == "int":
            value = int(value)
        elif field.type == "float":
            value = float(value)
        elif field.type == "bool":
            value = bool(value)
        else:
            value = str(value)
        setattr(ctx.config, key, value)


CONFIG_FIELDS = [
    ConfigField(
        key="max_sessions_per_exit",
        type="int",
        min=1,
        max=8,
        label="Workers (song song)",
        help="Song song requests per IP/exit. 2 is gentle, 6 is fast.",
    ),
    ConfigField(
        key="max_attempts",
        type="int",
        min=1,
        max=50,
        label="Max attempts",
        help="How many times a request is retried before giving up.",
    ),
    ConfigField(
        key="max_rotations",
        type="int",
        min=0,
        max=20,
        label="Max rotations",
        help="Max address rotations when an exit is blocked.",
    ),
    ConfigField(
        key="solve_timeout",
        type="float",
        min=1,
        max=600,
        label="Solve timeout (s)",
        help="How long a captcha/challenge may take in the browser.",
    ),
    ConfigField(
        key="archive_max_age",
        type="float",
        min=0,
        max=86400 * 30,
        label="Archive max age (s)",
        help="Max age of a cached page (wayback) used instead of a live fetch.",
    ),
    ConfigField(key="use_archive", type="bool", label="Use archive", help="Fall back to the wayback machine when a live fetch fails."),
    ConfigField(
        key="impersonate",
        type="text",
        label="Impersonate",
        help="Browser identity to impersonate (empty = default).",
    ),
    ConfigField(
        key="browser_mode",
        type="text",
        label="Browser mode",
        help="auto | headed | headless — how the challenge solver runs.",
    ),
    ConfigField(key="ignore_images", type="bool", label="Ignore images", help="Skip image downloads during extraction."),
]


@app.get("/api/config", response_model=List[ConfigField])
def get_config() -> List[ConfigField]:
    return [f.model_copy(update={"value": getattr(ctx.config, f.key, f.default)}) for f in CONFIG_FIELDS]


@app.post("/api/config")
def set_config(payload: Dict[str, Any]) -> Dict[str, Any]:
    fields = {f.key: f for f in CONFIG_FIELDS}
    errors: Dict[str, str] = {}
    updated: Dict[str, Any] = {}
    for key, raw in payload.items():
        field = fields.get(key)
        if field is None:
            errors[key] = "Unknown setting"
            continue
        try:
            if field.type == "int":
                value = int(raw)
            elif field.type == "float":
                value = float(raw)
            elif field.type == "bool":
                value = bool(raw)
            else:
                value = str(raw)
            if isinstance(value, (int, float)):
                if field.min is not None and value < field.min:
                    raise ValueError(f"Minimum is {field.min:g}")
                if field.max is not None and value > field.max:
                    raise ValueError(f"Maximum is {field.max:g}")
        except (TypeError, ValueError) as e:
            errors[key] = str(e)
            continue
        setattr(ctx.config, key, value)
        updated[key] = value
    if updated:
        _persist_settings(updated)
    if errors:
        raise HTTPException(status_code=422, detail=errors)
    return updated


def _run_job(job_id: str, req: ExtractRequest) -> None:
    """Execute a crawl job, recording every stage into the job's log."""
    job = JOBS.get_ref(job_id)
    if job is None:  # evicted before it started
        return
    _install_log_forwarding(job)

    crawler = None
    try:
        _set(job, status="running")
        _log(job, f"Looking up a crawler for {req.url}")

        from .services.sources import Sources

        crawler = Sources().init_crawler(req.url)
        _set(job, source=type(crawler).__name__)
        _log(job, f"Source detected: {type(crawler).__name__}")

        # Optional concurrency: clamp to the source's own ceiling so gentle
        # sources (e.g. 1qxs, max_sessions_per_exit=1) always override.
        wanted = req.workers or (ctx.config.max_sessions_per_exit + 1)
        workers = max(1, min(int(wanted), crawler.max_workers()))
        if workers != crawler.taskman.workers:
            crawler.taskman.init_executor(workers)
        _log(job, f"Workers: {workers}")

        if req.rate_limit:
            from .services.scraper import ctx_scraper

            ctx_scraper.set_rate_limit(req.url, req.rate_limit)
            _log(job, f"Rate limit set to {req.rate_limit} req/s")

        _log(job, "Fetching novel page...")
        novel = Novel(url=req.url)
        crawler.read_novel(novel)
        crawler.format_novel(novel)
        if not novel.title:
            raise LNException("No novel title found")

        _set(
            job,
            title=novel.title,
            author=novel.author,
            cover_url=novel.cover_url,
            language=novel.language,
            synopsis=novel.synopsis,
            tags=list(novel.tags),
            total_chapters=len(novel.chapters),
        )
        _log(job, f"Title: {novel.title}")
        if novel.author:
            _log(job, f"Author: {novel.author}")
        if novel.cover_url:
            _log(job, f"Cover: {novel.cover_url}")
        _log(job, f"Chapter list parsed: {len(novel.chapters)} chapters")

        book_id = ""
        if req.save:
            book_id = LIBRARY.save_book_meta(
                {
                    "url": novel.url,
                    "title": novel.title,
                    "author": novel.author,
                    "cover_url": novel.cover_url,
                    "language": novel.language,
                    "synopsis": novel.synopsis,
                    "tags": list(novel.tags),
                    "toc": [{"id": c.id, "title": c.title, "url": c.url} for c in novel.chapters],
                }
            )
            _set(job, book_id=book_id)
            _log(job, f"Saved to library as '{book_id}'")
            if req.overwrite:
                _log(job, "Overwrite mode: re-crawled chapters replace saved copies")
            cover_file = LIBRARY.cover_path(book_id)
            if novel.cover_url and not cover_file.is_file():
                try:
                    crawler.download_cover(novel.cover_url, cover_file)
                    _log(job, "Cover saved to library")
                except Exception as e:
                    _log(job, f"Cover download failed: {e}", level="warning")

        chapters = list(novel.chapters)
        requested = "all"
        if req.first:
            chapters = chapters[: req.first]
            requested = f"first {req.first}"
            _log(job, f"Scoped to the first {req.first} chapters")
        elif req.last:
            chapters = chapters[-req.last :]
            requested = f"last {req.last}"
            _log(job, f"Scoped to the last {req.last} chapters")
        _set(job, requested=requested)
        if req.only_ids:
            wanted = set(req.only_ids)
            chapters = [c for c in chapters if c.id in wanted]
            requested = f"{len(chapters)} missing"
            _set(job, requested=requested)
            _log(job, f"Fetch missing: {len(chapters)} chapters to download")
        if not chapters:
            raise LNException("No chapters to download")

        index_by_id = {c.id: i for i, c in enumerate(chapters)}
        with JOBS.lock:
            job.chapters = [
                JobChapter(id=c.id, title=c.title, url=c.url, success=False) for c in chapters
            ]
        _log(job, f"Downloading {len(chapters)} chapters...")

        futures = {
            crawler.taskman.submit_task(lambda ch: _fetch_chapter(crawler, ch), chapter): chapter
            for chapter in chapters
        }
        done_count = 0
        for chapter in crawler.taskman.resolve(
            futures=futures, disable_bar=True, desc="Chapters", unit="chap"
        ):
            if chapter is None:
                continue
            done_count += 1
            idx = index_by_id.get(chapter.id)
            if idx is None:
                continue
            error = chapter.get("error")
            with JOBS.lock:
                job.chapters[idx].success = bool(chapter.success)
                job.chapters[idx].error = error
            if req.save and book_id and chapter.success:
                if LIBRARY.save_chapter(book_id, chapter.to_dict(), overwrite=req.overwrite):
                    with JOBS.lock:
                        job.saved_count += 1
            title = (chapter.title or "").strip()[:60]
            elapsed = chapter.get("elapsed") or 0.0
            if chapter.success:
                _log(job, f"✓ Ch {chapter.id} · {title} ({elapsed:.1f}s)")
            else:
                _log(job, f"✗ Ch {chapter.id} · {title}: {error}", level="error")

        success_count = sum(1 for c in chapters if c.success)
        failed_count = len(chapters) - success_count
        _set(
            job,
            status="done",
            success_count=success_count,
            failed_count=failed_count,
            finished_at=time.time(),
        )
        level = "warning" if failed_count else "info"
        summary = f"Completed: {success_count} ok, {failed_count} failed"
        if req.save and book_id:
            summary += f", {job.saved_count} saved to library"
        _log(job, summary, level=level)
    except LNException as e:
        _set(job, status="failed", error=str(e), finished_at=time.time())
        _log(job, f"Failed: {e}", level="error")
    except Exception as e:
        logger.exception("Extract failed for %s", req.url)
        _set(job, status="failed", error=f"Extract failed: {e}", finished_at=time.time())
        _log(job, f"Failed: {e}", level="error")
    finally:
        if crawler is not None:
            crawler.close()
        _uninstall_log_forwarding()


def _fetch_chapter(crawler, chapter):
    started = time.perf_counter()

    def timed():
        chapter.elapsed = round(time.perf_counter() - started, 2)
        return chapter

    try:
        crawler.download_chapter(chapter)
        crawler.format_chapter(chapter)
    except Exception as e:
        logger.warning("Chapter %s failed: %s", chapter.id, e)
        chapter.success = False
        chapter.error = repr(e)
        return timed()
    chapter.error = None
    return timed()
