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

import logging
import threading
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from typing import Deque, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .context import APP_DIR, ctx
from .core import Novel
from .exceptions import LNException

logger = logging.getLogger(__name__)

_MAX_JOBS = 50
_MAX_LOG_LINES = 1000


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
    success_count: int = 0
    failed_count: int = 0
    error: Optional[str] = None
    started_at: float = 0.0
    finished_at: Optional[float] = None
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


JOBS = JobStore()


def _log(job: Job, message: str, level: str = "info") -> None:
    with JOBS.lock:
        job.logs.append(
            JobLog(t=round(time.time() - job.started_at, 2), level=level, message=message)
        )
        if len(job.logs) > _MAX_LOG_LINES:
            del job.logs[: len(job.logs) - _MAX_LOG_LINES]


def _set(job: Job, **fields) -> None:
    with JOBS.lock:
        for key, value in fields.items():
            setattr(job, key, value)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    ctx.setup(log_level=logging.WARNING)
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


def _run_job(job_id: str, req: ExtractRequest) -> None:
    """Execute a crawl job, recording every stage into the job's log."""
    job = JOBS.get_ref(job_id)
    if job is None:  # evicted before it started
        return

    crawler = None
    try:
        _set(job, status="running")
        _log(job, f"Looking up a crawler for {req.url}")

        from .services.sources import Sources

        crawler = Sources().init_crawler(req.url)
        _set(job, source=type(crawler).__name__)
        _log(job, f"Source detected: {type(crawler).__name__}")

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
            if not chapter.success:
                _log(
                    job,
                    f"Chapter {chapter.id} failed: {error}",
                    level="error",
                )
            elif done_count % 50 == 0:
                _log(job, f"Progress: {done_count}/{len(chapters)} chapters fetched")

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
        _log(job, f"Completed: {success_count} ok, {failed_count} failed", level=level)
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


def _fetch_chapter(crawler, chapter):
    try:
        crawler.download_chapter(chapter)
    except Exception as e:
        logger.warning("Chapter %s failed: %s", chapter.id, e)
        chapter.success = False
        chapter.error = repr(e)
        return chapter
    crawler.format_chapter(chapter)
    chapter.error = None
    return chapter
