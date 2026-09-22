"""Durable translation jobs. Run one ASGI process to share the global quota scheduler."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from ..context import APP_DIR
from .dictionary import load_legacy
from .models import MODELS, PARSER_VERSION, Inputs
from .parsing import ChapterValidationError, validate_inputs
from .pipeline import Pipeline
from .scheduler import Scheduler, api_keys, model_failure_summary
from .store import AlreadyRunning, Store

router = APIRouter(prefix="/api/translation", tags=["translation"])
ROOT = APP_DIR / "translations"
OUTPUT_NAMES = ("translated.json", "translated.txt", "dictionary.json", "ignored_dictionary.json")
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


def _preserved_output_path(job_id, filename):
    return ROOT / "_outputs" / job_id / filename


def _batch_metadata(store, progress):
    inputs = store.read("inputs.json", {}) or {}
    saved_metadata = store.read("batch-metadata.json", {}) or {}
    parsed = store.read("parsed-chapters.json", {}) or {}
    pairs = parsed.get("pairs", [])
    numbers = [pair[0].get("number") for pair in pairs if pair and pair[0].get("number") is not None]
    start = progress.get(
        "chapter_start", saved_metadata.get("chapter_start", min(numbers) if numbers else None)
    )
    end = progress.get(
        "chapter_end", saved_metadata.get("chapter_end", max(numbers) if numbers else None)
    )
    total = progress.get(
        "total_chapters", saved_metadata.get("total_chapters", len(numbers) or None)
    )
    source_name = (
        inputs.get("source_name")
        or inputs.get("book_title")
        or saved_metadata.get("source_name")
        or saved_metadata.get("book_title")
    )
    if source_name:
        source_name = Path(source_name).stem
    book = inputs.get("book_title") or saved_metadata.get("book_title") or source_name
    if book and Path(book).suffix:
        book = Path(book).stem
    if book and start is not None and end is not None:
        display_title = f"{book} · Ch. {start}–{end}"
    elif book and total is not None:
        display_title = f"{book} · {total} chapters"
    elif source_name:
        display_title = source_name
    else:
        display_title = f"Translation batch {store.id[:8]}"
    return {
        "display_title": display_title,
        "book_title": book,
        "chapter_start": start,
        "chapter_end": end,
        "total_chapters": total,
    }


STAGE_LABELS = {
    "Local parsing": "Parsing chapters",
    "Local chapter alignment": "Aligning RAW and VietPhrase",
    "Local terminology scan": "Discovering terminology",
    "Terminology candidates ready": "Resolving terminology",
    "Dictionary resolution": "Resolving terminology",
    "Terminology resolution": "Resolving terminology",
    "Reviewing ignored candidates": "Reviewing ignored candidates",
    "Dictionary frozen": "Freezing dictionary",
    "Translating": "Translating",
    "Local final global audit": "Final audit",
    "Complete": "Completed",
    "Queued": "Preparing inputs",
}


def _decorate_snapshot(store, progress):
    result = {**progress, **_batch_metadata(store, progress)}
    stage = progress.get("stage")
    result["stage_label"] = STAGE_LABELS.get(stage, stage or progress.get("status", "Unknown"))
    if progress.get("status") == "failed":
        result["stage_label"] = "Failed"
    elif progress.get("status") == "cancelled":
        result["stage_label"] = "Cancelled"
    elif progress.get("status") == "deleting":
        result["stage_label"] = "Deleting batch"
    if progress.get("status") == "failed":
        failures = sorted((store.path / "validation-failures").glob("*.json"))
        if failures:
            failure = store.read(str(failures[-1].relative_to(store.path)), {}) or {}
            findings = failure.get("findings", [])
            first = findings[0] if findings else {}
            result["failure_context"] = {
                "task": failure.get("task"),
                "repair_attempts": failure.get("repair_attempts", 0),
                "findings": len(findings),
                "location": first.get("location")
                or (
                    {
                        "chapter": first.get("chapter_number"),
                        "chunk": first.get("chunk_index"),
                    }
                    if first.get("chapter_number") is not None
                    else None
                ),
            }
    ignored_path = store.path / "ignored_dictionary.json"
    ignored_artifact = store.read("ignored_dictionary.json") if ignored_path.exists() else None
    result["ignored_dictionary_available"] = bool(
        ignored_artifact is not None and ignored_artifact.get("available", True)
    )
    if ignored_artifact is not None:
        result["ignored_dictionary_summary"] = ignored_artifact.get("summary", {})
    result["outputs"] = {
        "translation": f"/api/translation/jobs/{store.id}/outputs/translated.txt",
        "dictionary": f"/api/translation/jobs/{store.id}/outputs/dictionary.json",
        "ignored_dictionary": f"/api/translation/jobs/{store.id}/outputs/ignored_dictionary.json",
    }
    return result


def snapshot(store, include_logs=True):
    progress = store.read("progress.json", {"job_id": store.id, "status": "pending"})
    progress["request_statistics"] = store.request_statistics()
    report = store.read("dictionary-resolution-report.json")
    if report:
        progress["dictionary_report"] = report.get("summary", {})
        progress["dictionary_review"] = [
            entry
            for entry in report.get("entries", [])
            if entry.get("state") == "IGNORE"
            or entry.get("severity") not in (None, "INFO")
            or entry.get("resolution_status") not in (None, "locked")
        ][:10]
    if include_logs:
        progress["logs"] = store.logs()
        # Older checkpoints saved only the final provider error. Recover the
        # actual same-task chain from their persisted diagnostics for display.
        if progress.get("status") == "failed" and str(progress.get("error", "")).startswith(
            "Gemini HTTP"
        ):
            failed = [
                event
                for event in progress["logs"]
                if event.get("status") == "failed" and event.get("model")
            ]
            if failed:
                task = failed[-1].get("task")
                failures = {
                    event["model"]: event.get("error", "Provider failed")
                    for event in failed
                    if event.get("task") == task
                }
                if all(model in failures for model in MODELS):
                    progress["error"] = model_failure_summary(failures)
    if (
        progress["status"] in ("running", "pending")
        and store.id not in _tasks
        and not store.is_active()
    ):
        return _decorate_snapshot(
            store, {**progress, "status": "interrupted", "stage": "Ready to resume"}
        )
    return _decorate_snapshot(store, progress)


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
        validation_failures = sorted((store.path / "validation-failures").glob("*.json"))
        error_detail = (
            store.read(str(validation_failures[-1].relative_to(store.path)))
            if validation_failures
            else None
        )
        store.progress(
            "failed", error=str(exc), error_detail=error_detail, stage="Stopped; resume available"
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
    if not api_keys():
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
        "api_key_configured": bool(api_keys()),
        "api_key_count": len(api_keys()),
    }


@router.post("/jobs", status_code=202)
async def create(inputs: Inputs):
    try:
        pairs = validate_inputs(inputs.raw, inputs.vietphrase)
        load_legacy(inputs.dictionary)
    except ChapterValidationError as exc:
        raise HTTPException(422, exc.detail)
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, str(exc))
    # Omit absent optional display metadata so legacy callers derive the same
    # content identity as the pre-label batch schema; retain dictionary=None,
    # which was already part of the original API identity.
    store_inputs = inputs.model_dump()
    for key in ("book_title", "source_name"):
        if store_inputs.get(key) is None:
            store_inputs.pop(key, None)
    store = Store(ROOT, inputs=store_inputs)
    store.write(
        "batch-metadata.json",
        {
            "book_title": inputs.book_title,
            "source_name": inputs.source_name,
            "chapter_start": min((raw.number for raw, _ in pairs), default=None),
            "chapter_end": max((raw.number for raw, _ in pairs), default=None),
            "total_chapters": len(pairs),
        },
    )
    store.write("parser-version.json", {"version": PARSER_VERSION})
    return start(store)


@router.get("/jobs")
async def jobs():
    if not ROOT.exists():
        return []
    paths = sorted(ROOT.glob("*/progress.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return [snapshot(get_store(path.parent.name), include_logs=False) for path in paths[:100]]


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
    if not re.fullmatch(r"[a-f0-9]{64}", job_id):
        raise HTTPException(404, "Translation job not found")
    if filename not in OUTPUT_NAMES:
        raise HTTPException(404, "Unknown output")
    batch_path = ROOT / job_id
    if batch_path.is_dir():
        store = get_store(job_id)
        if store.read("progress.json", {}).get("status") != "done":
            raise HTTPException(409, "Outputs are available after final validation")
        path = store.path / filename
    else:
        path = _preserved_output_path(job_id, filename)
        if not path.exists():
            raise HTTPException(404, "Translation job not found")
    if not path.is_file():
        raise HTTPException(404, "Output is not available")
    media_type = "text/plain" if filename.endswith(".txt") else "application/json"
    return FileResponse(path, media_type=media_type, filename=filename)


@router.delete("/jobs/{job_id}")
async def delete_job(job_id: str, delete_outputs: bool = False):
    store = get_store(job_id)
    task = _tasks.get(job_id)
    store.progress("deleting", stage="Deleting batch")
    if task:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        _tasks.pop(job_id, None)
    elif store.is_active():
        store.write("cancel-request.json", {"requested": True})
        for _ in range(100):
            await asyncio.sleep(0.05)
            if not store.is_active():
                break
        if store.is_active():
            raise HTTPException(409, "Translation batch is still running; retry deletion")

    preserved = _preserved_output_path(job_id, "translated.txt").parent
    if not delete_outputs:
        generated = [store.path / name for name in OUTPUT_NAMES if (store.path / name).exists()]
        if generated:
            preserved.mkdir(parents=True, exist_ok=True)
            for path in generated:
                shutil.copy2(path, preserved / path.name)
    elif preserved.exists():
        shutil.rmtree(preserved)

    if store.path.parent != ROOT or store.path.name != job_id:
        raise HTTPException(400, "Invalid translation batch path")
    shutil.rmtree(store.path)
    return {
        "job_id": job_id,
        "status": "deleted",
        "outputs_preserved": not delete_outputs,
    }


@router.get("/jobs/{job_id}/dictionary-report")
async def dictionary_report(job_id: str):
    report = get_store(job_id).read("dictionary-resolution-report.json")
    if report is None:
        raise HTTPException(404, "Dictionary report is not available")
    return report


async def shutdown():
    tasks = list(_tasks.values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
