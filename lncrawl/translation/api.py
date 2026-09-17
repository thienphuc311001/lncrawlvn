"""Durable translation jobs. Run one ASGI process to share the global quota scheduler."""

from __future__ import annotations

import asyncio
import os
import re

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from ..context import APP_DIR
from .dictionary import load_legacy
from .models import MODELS, PARSER_VERSION, Inputs
from .parsing import ChapterValidationError, validate_inputs
from .pipeline import Pipeline
from .scheduler import Scheduler
from .store import AlreadyRunning, Store

router = APIRouter(prefix="/api/translation", tags=["translation"])
ROOT = APP_DIR / "translations"
_tasks = {}
_scheduler = None


def scheduler():
    global _scheduler
    if _scheduler is None:
        _scheduler = Scheduler(concurrency=int(os.getenv("TRANSLATION_WORKERS", "2")))
    return _scheduler


def get_store(job_id):
    if not re.fullmatch(r"[a-f0-9]{64}", job_id):
        raise HTTPException(404, "Translation job not found")
    try:
        return Store(ROOT, job_id=job_id)
    except FileNotFoundError:
        raise HTTPException(404, "Translation job not found")


def snapshot(store):
    progress = store.read("progress.json", {"job_id": store.id, "status": "pending"})
    if (
        progress["status"] in ("running", "pending")
        and store.id not in _tasks
        and not store.is_active()
    ):
        return {**progress, "status": "interrupted", "stage": "Ready to resume"}
    return progress


async def run(store):
    try:
        with store.execution():
            await Pipeline(store, scheduler()).run()
    except AlreadyRunning:
        pass  # A CLI or another API process owns this batch; leave its progress intact.
    except asyncio.CancelledError:
        store.progress("cancelled", stage="Cancelled; completed checkpoints preserved")
    except ChapterValidationError as exc:
        store.progress(
            "failed",
            error=str(exc),
            error_detail=exc.detail,
            stage="Input validation failed; resubmit corrected files",
        )
    except Exception as exc:
        store.progress(
            "failed", error=str(exc), error_detail=None, stage="Stopped; resume available"
        )
    finally:
        _tasks.pop(store.id, None)


def start(store):
    if (
        store.id in _tasks
        or store.is_active()
        or store.read("progress.json", {}).get("status") == "done"
    ):
        return snapshot(store)
    if not os.getenv("GOOGLE_AI_API_KEY"):
        raise HTTPException(503, "Set GOOGLE_AI_API_KEY on the server before starting translation")
    scheduler()  # Validate configuration before accepting work.
    store.progress("pending", stage="Queued", error=None, error_detail=None)
    _tasks[store.id] = asyncio.create_task(run(store))
    return snapshot(store)


@router.get("/config")
async def config():
    return {
        "models": dict(zip(("primary", "fallback", "backup"), MODELS)),
        "concurrency": scheduler().concurrency,
        "stagger_ms": 300,
        "api_key_configured": bool(os.getenv("GOOGLE_AI_API_KEY")),
    }


@router.post("/jobs", status_code=202)
async def create(inputs: Inputs):
    try:
        validate_inputs(inputs.raw, inputs.vietphrase)
        load_legacy(inputs.dictionary)
    except ChapterValidationError as exc:
        raise HTTPException(422, exc.detail)
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, str(exc))
    store = Store(ROOT, inputs=inputs.model_dump())
    store.write("parser-version.json", {"version": PARSER_VERSION})
    return start(store)


@router.get("/jobs")
async def jobs():
    if not ROOT.exists():
        return []
    paths = sorted(ROOT.glob("*/progress.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return [snapshot(get_store(path.parent.name)) for path in paths[:100]]


@router.get("/jobs/{job_id}")
async def job(job_id: str):
    return snapshot(get_store(job_id))


@router.post("/jobs/{job_id}/resume", status_code=202)
async def resume(job_id: str):
    store = get_store(job_id)
    if store.read("progress.json", {}).get("status") == "done":
        return snapshot(store)
    if store.read("parser-version.json") != {"version": PARSER_VERSION}:
        raise HTTPException(
            409,
            "This unfinished job uses a missing or outdated chapter parser version. "
            "Resubmit the original RAW and VIETPHRASE files as a new job; "
            "legacy checkpoints cannot be resumed safely.",
        )
    inputs = store.read("inputs.json")
    try:
        validate_inputs(inputs["raw"], inputs["vietphrase"])
    except ChapterValidationError as exc:
        raise HTTPException(422, exc.detail)
    return start(store)


@router.post("/jobs/{job_id}/cancel")
async def cancel(job_id: str):
    store = get_store(job_id)
    task = _tasks.get(job_id)
    if task:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        _tasks.pop(store.id, None)
        store.progress("cancelled", stage="Cancelled; completed checkpoints preserved")
    elif store.is_active():
        store.write("cancel-request.json", {"requested": True})
    return snapshot(store)


@router.get("/jobs/{job_id}/outputs/{filename}")
async def output(job_id: str, filename: str):
    store = get_store(job_id)
    if filename not in ("translated.json", "dictionary.json"):
        raise HTTPException(404, "Unknown output")
    if store.read("progress.json", {}).get("status") != "done":
        raise HTTPException(409, "Outputs are available after final validation")
    return FileResponse(store.path / filename, media_type="application/json", filename=filename)


async def shutdown():
    tasks = list(_tasks.values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
