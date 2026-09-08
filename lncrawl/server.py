"""FastAPI server exposing the crawl pipeline over HTTP.

Development wiring: ``lnmini run dev`` starts this alongside the Next.js
frontend (see ``scripts/lnmini.sh``). The CLI in ``lncrawl.app`` remains the
primary interface.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .context import APP_DIR, ctx
from .core import Novel
from .exceptions import LNException

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    ctx.setup(log_level=logging.WARNING)
    logger.info("lncrawl-mini API ready (data dir: %s)", APP_DIR)
    yield
    from .services.scraper import ctx_scraper

    ctx_scraper.close()


app = FastAPI(
    title="lncrawl-mini",
    description="Minimal web-novel extraction API.",
    version="0.1.0",
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
    include_body: bool = Field(False, description="Include chapter HTML bodies.")
    rate_limit: Optional[float] = Field(None, gt=0, description="Requests per second cap.")


class ChapterOut(BaseModel):
    id: int
    url: str
    title: str
    volume: Optional[int] = None
    body: Optional[str] = None


class VolumeOut(BaseModel):
    id: int
    title: str
    chapters: int


class ExtractResponse(BaseModel):
    url: str
    title: str
    author: str = ""
    cover_url: str = ""
    language: Optional[str] = None
    synopsis: str = ""
    tags: List[str] = []
    volumes: List[VolumeOut] = []
    chapters: List[ChapterOut] = []
    failed_chapters: int = 0


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/api/extract", response_model=ExtractResponse)
def extract(req: ExtractRequest) -> ExtractResponse:
    """Crawl a novel URL and return its metadata and chapter list.

    Runs the same pipeline as the CLI: source registry -> crawler -> read ->
    format -> parallel chapter download. The endpoint is a plain ``def`` so the
    blocking crawl runs in FastAPI's threadpool.
    """
    from .services.sources import Sources

    crawler = None
    try:
        crawler = Sources().init_crawler(req.url)

        if req.rate_limit:
            from .services.scraper import ctx_scraper

            ctx_scraper.set_rate_limit(req.url, req.rate_limit)

        novel = Novel(url=req.url)
        crawler.read_novel(novel)
        crawler.format_novel(novel)
        if not novel.title:
            raise LNException("No novel title found")

        chapters = list(novel.chapters)
        if req.first:
            chapters = chapters[: req.first]
        elif req.last:
            chapters = chapters[-req.last :]
        if not chapters:
            raise LNException("No chapters to download")

        futures = {
            crawler.taskman.submit_task(lambda ch: _fetch_chapter(crawler, ch), chapter): chapter
            for chapter in chapters
        }
        list(crawler.taskman.resolve(futures=futures, disable_bar=True, desc="Chapters", unit="chap"))

        return ExtractResponse(
            url=req.url,
            title=novel.title,
            author=novel.author,
            cover_url=novel.cover_url,
            language=novel.language,
            synopsis=novel.synopsis,
            tags=list(novel.tags),
            volumes=[
                VolumeOut(id=v.id, title=v.title, chapters=v.chapters) for v in novel.volumes
            ],
            chapters=[
                ChapterOut(
                    id=c.id,
                    url=c.url,
                    title=c.title,
                    volume=c.volume,
                    body=c.body if req.include_body else None,
                )
                for c in chapters
            ],
            failed_chapters=sum(1 for c in chapters if not c.success),
        )
    except LNException as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("Extract failed for %s", req.url)
        raise HTTPException(status_code=500, detail=f"Extract failed: {e}")
    finally:
        if crawler is not None:
            crawler.close()


def _fetch_chapter(crawler, chapter):
    try:
        crawler.download_chapter(chapter)
    except Exception as e:
        logger.warning("Chapter %s failed: %s", chapter.id, e)
        chapter.success = False
        return chapter
    crawler.format_chapter(chapter)
    return chapter
