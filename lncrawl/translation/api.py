"""Durable translation jobs. Run one ASGI process to share the global quota scheduler."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from ..context import APP_DIR
from .dictionary import load_dictionary, load_unresolved
from .manual_review import dictionary_fingerprint, review_fingerprint, review_path
from .models import MODELS, PARSER_VERSION, PIPELINE_VERSION, Inputs, Translation
from .notes import author_note_policy
from .parsing import ChapterValidationError, parse_chapters, validate_inputs
from .pipeline import Pipeline
from .scheduler import Scheduler, api_keys, model_failure_summary
from .store import AlreadyRunning, Store, atomic_json, digest
from .validation import local_findings, repair_integrity_findings

router = APIRouter(prefix="/api/translation", tags=["translation"])
ROOT = APP_DIR / "translations"
OUTPUT_NAMES = ("translated.json", "translated.txt", "dictionary.json", "unresolved.json",
                "author-notes.txt")
_tasks = {}
_scheduler = None


class ManualReviewDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    chapter: int
    paragraph_id: str = Field(pattern=r"^P\d{4,}_\d{4}$")
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    action: Literal["accept", "replace"]
    text: str | None = Field(default=None, max_length=200_000)


def _failure_detail_for_error(store, error):
    match = re.search(r"Chapter\s+(\d+).*?(P\d{4,}_\d{4})", str(error or ""))
    if not match:
        return None
    failure = store.read(f"validation-failures/{int(match[1])}.json") or {}
    return failure if failure.get("paragraph_id") == match[2] else None


def _manual_review_context(store, progress=None):
    progress = progress or store.read("progress.json", {}) or {}
    if progress.get("status") != "failed":
        return None
    match = re.search(r"Chapter\s+(\d+).*?(P\d{4,}_\d{4})", str(progress.get("error", "")))
    if not match:
        return None
    number, identifier = int(match[1]), match[2]
    inputs = store.read("inputs.json") or {}
    if not isinstance(inputs.get("raw"), str):
        return None
    try:
        chapters = parse_chapters(inputs["raw"])
    except ChapterValidationError:
        return None
    chapter = next((item for item in chapters
                    if item.number == number), None)
    if chapter is None or identifier not in dict(chapter.paragraph_items):
        return None
    checkpoint = store.read("post-dictionary.json") or store.read("pre-dictionary.json") or {}
    dictionary = load_dictionary(checkpoint.get("dictionary", inputs.get("dictionary")))
    pre_hash = digest(load_dictionary(
        (store.read("pre-dictionary.json") or {}).get("dictionary", inputs.get("dictionary"))))
    selected = None
    # Final dictionary review operates on committed chapters; normal translation
    # failures operate on pending chapters. The committed copy is what resume will
    # actually re-read when both checkpoints exist.
    for checkpoint_path in (f"chapters/{number}.json", f"pending-chapters/{number}.json"):
        candidate = store.read(checkpoint_path)
        if (not candidate or candidate.get("raw_hash") != digest((chapter.title, chapter.paragraphs))
                or candidate.get("pre_dictionary_hash") != pre_hash):
            continue
        try:
            result = Translation.model_validate(candidate["translation"])
        except (KeyError, TypeError, ValueError):
            continue
        if any(item.id == identifier for item in result.segments):
            selected = (checkpoint_path, candidate, result)
            break
    if selected is None:
        return None
    checkpoint_path, saved, result = selected
    segment = next((item for item in result.segments if item.id == identifier), None)
    if segment is None:
        return None
    raw = dict(chapter.paragraph_items)[identifier]
    findings = [item for item in local_findings(chapter, result, dictionary)
                if item.get("id") == identifier]
    if not findings:
        # A targeted semantic QA rejection can have no remaining deterministic
        # finding. Still give the reviewer a way to resolve that failed repair.
        failure = _failure_detail_for_error(store, progress.get("error")) or {}
        if (failure.get("paragraph_id") == identifier
                and failure.get("original_text") == segment.text):
            findings = [item for item in failure.get("findings", [])
                        if item.get("id", identifier) == identifier]
        if not findings:
            findings = [{"kind": "manual_repair_failed", "id": identifier,
                         "message": str(progress.get("error", "Repair failed"))}]
    return {
        "chapter": number, "paragraph_id": identifier, "raw": raw,
        "current_text": segment.text, "findings": findings,
        "fingerprint": review_fingerprint(raw, segment.text, dictionary, findings),
        "dictionary": dictionary, "checkpoint": saved,
        "checkpoint_path": checkpoint_path, "result": result,
    }


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


def _download_stem(metadata, job_id):
    for value in (metadata.get("source_name"), metadata.get("book_title")):
        if not isinstance(value, str) or not value.strip():
            continue
        basename = value.strip().replace("\\", "/").rsplit("/", 1)[-1]
        stem = Path(basename).stem
        safe = re.sub(r"[^\w.-]+", "-", stem, flags=re.UNICODE).strip(".-_")
        safe = safe.encode("utf-8")[:180].decode("utf-8", "ignore").strip(".-_")
        if safe:
            return safe
    return f"translation-{job_id[:8]}"


def _download_names(metadata, job_id):
    stem = _download_stem(metadata, job_id)
    return {
        "translation": f"{stem}-translated.txt",
        "translation_json": f"{stem}-translated.json",
        "dictionary": f"{stem}-dictionary.json",
        "unresolved": f"{stem}-unresolved.json",
        "author_notes": f"{stem}-author-notes.txt",
    }


def _download_metadata(store):
    inputs = store.read("inputs.json", {}) or {}
    saved = store.read("batch-metadata.json", {}) or {}
    return {key: inputs.get(key) or saved.get(key) for key in ("source_name", "book_title")}


def _batch_metadata(store, progress):
    inputs = store.read("inputs.json", {}) or {}
    saved_metadata = store.read("batch-metadata.json", {}) or {}
    parsed = store.read("parsed-chapters.json", {}) or {}
    chapters = parsed.get("chapters", [])
    numbers = [item.get("number") for item in chapters if item.get("number") is not None]
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
    "Parsing RAW": "Parsing RAW",
    "Local parsing": "Parsing chapters",
    "Pre-dictionary resolution": "Resolving terminology",
    "Post-dictionary review": "Reviewing remaining terminology",
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
    result["output_filenames"] = _download_names(_download_metadata(store), store.id)
    stage = progress.get("stage")
    result["stage_label"] = STAGE_LABELS.get(stage, stage or progress.get("status", "Unknown"))
    if progress.get("status") == "failed":
        result["stage_label"] = "Failed"
    elif progress.get("status") == "cancelled":
        result["stage_label"] = "Cancelled"
    elif progress.get("status") == "deleting":
        result["stage_label"] = "Deleting batch"
    if progress.get("status") == "failed":
        failure = _failure_detail_for_error(store, progress.get("error"))
        if failure:
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
    result["unresolved_terms"] = len(store.read("unresolved.json", []) or [])
    result["outputs"] = {
        "translation": f"/api/translation/jobs/{store.id}/outputs/translated.txt",
        "dictionary": f"/api/translation/jobs/{store.id}/outputs/dictionary.json",
        "unresolved": f"/api/translation/jobs/{store.id}/outputs/unresolved.json",
    }
    if (store.path / "author-notes.txt").is_file():
        result["outputs"]["author_notes"] = f"/api/translation/jobs/{store.id}/outputs/author-notes.txt"
    result["author_note_policy"] = (store.read("author-note-policy.json") or {}).get("policy", "preserve")
    return result


def snapshot(store, include_logs=True):
    progress = store.read("progress.json", {"job_id": store.id, "status": "pending"})
    if progress.get("status") == "failed" and re.search(
        r"Chapter\s+\d+.*?P\d{4,}_\d{4}", str(progress.get("error", ""))
    ):
        progress["error_detail"] = _failure_detail_for_error(store, progress.get("error"))
    progress["request_statistics"] = store.request_statistics()
    progress["request_warning"] = store.read("request-warning.json")
    dictionary = store.read("dictionary.json")
    if dictionary is None:
        dictionary = (store.read("pre-dictionary.json") or {}).get("dictionary")
    if dictionary is not None:
        progress["dictionary_report"] = {
            "confirmed_terms": len(dictionary if isinstance(dictionary, list) else dictionary.get("entries", [])),
            "unresolved_terms": len(store.read("unresolved.json", []) or []),
        }
    if include_logs:
        progress["logs"] = store.logs()
        review = _manual_review_context(store, progress)
        if review:
            progress["manual_review"] = {
                key: review[key] for key in (
                    "chapter", "paragraph_id", "raw", "current_text",
                    "findings", "fingerprint",
                )
            }
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
        error_detail = _failure_detail_for_error(store, str(exc))
        store.progress(
            "failed", error=str(exc), error_detail=error_detail, stage="Stopped; resume available"
        )
    finally:
        _tasks.pop(store.id, None)


def start(store):
    if (
        store.id in _tasks
        or store.is_active()
        or (store.read("progress.json", {}).get("status") == "done"
            and store.read("pipeline-version.json", {}).get("version") == PIPELINE_VERSION)
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
        "models": dict(zip(("primary", "fallback"), MODELS)),
        "concurrency": scheduler().concurrency,
        "stagger_ms": 300,
        "api_key_configured": bool(api_keys()),
        "api_key_count": len(api_keys()),
        "author_note_policy": author_note_policy(),
    }


@router.post("/jobs", status_code=202)
async def create(inputs: Inputs):
    try:
        chapters = validate_inputs(inputs.raw)
        load_dictionary(inputs.dictionary)
        load_unresolved(inputs.dictionary)
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
            "chapter_start": min((chapter.number for chapter in chapters), default=None),
            "chapter_end": max((chapter.number for chapter in chapters), default=None),
            "total_chapters": len(chapters),
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
    if (store.read("progress.json", {}).get("status") == "done"
            and store.read("pipeline-version.json", {}).get("version") == PIPELINE_VERSION):
        return snapshot(store)
    inputs = store.read("inputs.json")
    try:
        validate_inputs(inputs["raw"])
    except ChapterValidationError as exc:
        raise HTTPException(422, exc.detail)
    store.write("parser-version.json", {"version": PARSER_VERSION})
    return start(store)


@router.post("/jobs/{job_id}/review", status_code=202)
async def review(job_id: str, decision: ManualReviewDecision):
    """Resolve one failed paragraph without changing a locked dictionary."""
    store = get_store(job_id)
    try:
        with store.execution():
            context = _manual_review_context(store)
            if not context:
                raise HTTPException(409, "No current paragraph is waiting for manual review")
            if (decision.chapter != context["chapter"]
                    or decision.paragraph_id != context["paragraph_id"]
                    or decision.fingerprint != context["fingerprint"]):
                raise HTTPException(409, "Review changed; refresh the batch before deciding")
            raw = context["raw"]
            current = context["current_text"]
            dictionary = context["dictionary"]
            identifier = context["paragraph_id"]
            number = context["chapter"]
            now = datetime.now(timezone.utc).isoformat()
            if decision.action == "replace":
                replacement = (decision.text or "").strip()
                if not replacement or replacement == current:
                    raise HTTPException(422, "Enter a changed, complete replacement paragraph")
                rejected = repair_integrity_findings(
                    identifier, raw, current, replacement, dictionary)
                if rejected:
                    raise HTTPException(422, {"message": "Replacement did not pass local validation",
                                              "findings": rejected})
                saved = context["checkpoint"]
                result = context["result"]
                next(item for item in result.segments if item.id == identifier).text = replacement
                saved["translation"] = result.model_dump()
                if context["checkpoint_path"].startswith("pending-chapters/"):
                    saved["state"] = "validated_repair_pending_commit"
                store.write(context["checkpoint_path"], saved)
                history_name = f"revisions/{number}/{identifier}.json"
                history = store.read(history_name, []) or []
                if not history:
                    history.append({"revision": 0, "text": current, "kind": "initial"})
                history.append({"revision": len(history), "text": replacement,
                                "kind": "manual_replacement", "timestamp": now})
                store.write(history_name, history)
                record = {"decision": "replace", "chapter": number, "id": identifier,
                          "raw_hash": digest(raw), "before_hash": digest(current),
                          "text_hash": digest(replacement), "timestamp": now}
            else:
                if decision.text is not None:
                    raise HTTPException(422, "Accept current does not take replacement text")
                record = {"decision": "accept", "chapter": number, "id": identifier,
                          "raw_hash": digest(raw), "text_hash": digest(current),
                          "dictionary_hash": dictionary_fingerprint(dictionary, raw),
                          "accepted_findings": context["findings"], "timestamp": now}
            store.write(review_path(number, identifier), record)
            store.log({"event": "MANUAL_REVIEW_DECISION", "status": "manual_review_decision",
                       "chapter": number, "paragraph_id": identifier,
                       "decision": decision.action, "findings": context["findings"]})
    except AlreadyRunning as exc:
        raise HTTPException(409, str(exc)) from exc
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
        metadata = _download_metadata(store)
    else:
        path = _preserved_output_path(job_id, filename)
        if not path.exists():
            raise HTTPException(404, "Translation job not found")
        metadata_path = path.parent / "download-metadata.json"
        if metadata_path.is_file():
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        else:
            metadata = {}
    if not path.is_file():
        raise HTTPException(404, "Output is not available")
    media_type = "text/plain" if filename.endswith(".txt") else "application/json"
    name_key = {
        "translated.txt": "translation", "translated.json": "translation_json",
        "dictionary.json": "dictionary", "unresolved.json": "unresolved",
        "author-notes.txt": "author_notes",
    }[filename]
    download_name = _download_names(metadata, job_id)[name_key]
    return FileResponse(path, media_type=media_type, filename=download_name)


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
            atomic_json(preserved / "download-metadata.json", _download_metadata(store))
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
    store = get_store(job_id)
    dictionary = store.read("dictionary.json")
    if dictionary is None:
        dictionary = (store.read("pre-dictionary.json") or {}).get("dictionary")
    if dictionary is None:
        raise HTTPException(404, "Dictionary report is not available")
    return {"summary": {"confirmed_terms": len(dictionary if isinstance(dictionary, list) else dictionary.get("entries", [])),
                        "unresolved_terms": len(store.read("unresolved.json", []) or [])},
            "unresolved": store.read("unresolved.json", []) or []}


async def shutdown():
    tasks = list(_tasks.values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
