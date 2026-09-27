"""Adapt saved library chapters to the existing durable translation job pipeline."""

from __future__ import annotations

import re
import threading
import uuid
from contextlib import contextmanager
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

from .exceptions import LNException
from .library import LIBRARY
from .translation import api as translation_api
from .translation.dictionary import load_dictionary, load_rejected, load_unresolved
from .translation.manual_review import dictionary_fingerprint
from .translation.models import DICTIONARY_VERSION, Inputs
from .translation.parsing import (
    HEADING,
    REFERENCE,
    ChapterValidationError,
    chapter_number,
    split_inline_body,
    validate_inputs,
)
from .translation.store import digest
from .utils.html_tools import extract_text

router = APIRouter(prefix="/api/books", tags=["book translation"])
_SAVED_TITLE_WRAPPER = re.compile(
    r"^\s*(?:Chương|Chapter)\s*(\d+)(?:\s*[:：.\-]\s*|\s+)(.*)$", re.I,
)
_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


class DictionaryImport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dictionary: dict | list


class TranslateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    refresh: bool = False


@contextmanager
def _book_lock(book_id: str):
    with _locks_guard:
        lock = _locks.setdefault(book_id, threading.RLock())
    with lock:
        yield


def _book(book_id: str) -> dict:
    try:
        LIBRARY._guarded_book_dir(book_id)
        book = LIBRARY._read_meta(book_id)
    except LNException as exc:
        raise HTTPException(400, str(exc)) from exc
    if book is None:
        raise HTTPException(404, "Book not found in library")
    return book


def _normalize_dictionary(value: Any) -> dict:
    try:
        return {
            **load_dictionary(value),
            "unresolved": load_unresolved(value),
            "rejected": load_rejected(value),
        }
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, str(exc)) from exc


def _state(book_id: str) -> dict:
    try:
        stored = LIBRARY.load_translation_state(book_id)
    except LNException as exc:
        raise HTTPException(500, str(exc)) from exc
    if stored is None:
        return {
            "dictionary": {"version": DICTIONARY_VERSION, "entries": [],
                           "unresolved": [], "rejected": []},
            "imported": False, "active": None, "last_error": None,
        }
    return stored


def _save_state(book_id: str, state: dict) -> None:
    try:
        LIBRARY.save_translation_state(book_id, state)
    except LNException as exc:
        raise HTTPException(500, str(exc)) from exc


def _raw(book_id: str, chapter_id: int) -> str:
    try:
        chapter = LIBRARY.load_chapter(book_id, chapter_id)
    except LNException as exc:
        raise HTTPException(400, str(exc)) from exc
    if chapter is None:
        raise HTTPException(404, "Chapter not saved yet")
    title = chapter.get("title") or ""
    wrapper = _SAVED_TITLE_WRAPPER.match(title)
    printed_number = int(wrapper[1]) if wrapper else None
    if printed_number == chapter_id:
        title = wrapper[2].strip()
    lines = []
    has_prose = False
    for line in extract_text(chapter.get("body") or "").splitlines():
        heading = HEADING.fullmatch(line.strip())
        if (heading and not REFERENCE.match(heading[2])
                and (chapter_number(heading[1]) == chapter_id
                     or (not has_prose and printed_number is not None
                         and chapter_number(heading[1]) == printed_number))):
            # The TOC ID can be an ordinal while the printed chapter number
            # differs. Match the saved title's number only at the body start.
            source_title, inline_body = split_inline_body(heading[2].strip())
            if source_title and not has_prose:
                title = source_title
            if inline_body:
                lines.append(inline_body)
                has_prose = True
            continue
        lines.append(line)
        has_prose = has_prose or bool(line.strip())
    raw = f"第{chapter_id}章 {title}\n" + "\n".join(lines)
    if len(raw) > 20_000_000:
        raise HTTPException(422, "Chapter exceeds the translation RAW limit")
    try:
        chapters = validate_inputs(raw)
    except ChapterValidationError as exc:
        raise HTTPException(422, exc.detail) from exc
    if len(chapters) != 1 or chapters[0].number != chapter_id:
        raise HTTPException(422, "Saved chapter must contain exactly its own chapter body")
    return raw


def _reconcile(book_id: str, state: dict) -> dict | None:
    """Commit validated job outputs once, before any subsequent book action."""
    active = state.get("active")
    if not active:
        return None
    try:
        job = translation_api.snapshot(translation_api.get_store(active["job_id"]))
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
        state["active"] = None
        state["last_error"] = "Translation job no longer exists; start translation again"
        state["last_error_chapter_id"] = active["chapter_id"]
        _save_state(book_id, state)
        return None
    status = job["status"]
    if status == "cancelled":
        state["active"] = None
        _save_state(book_id, state)
    elif status == "done":
        store = translation_api.get_store(active["job_id"])
        output = store.read("translated.json")
        dictionary_output = store.read("dictionary.json")
        chapters = output.get("chapters") if isinstance(output, dict) else None
        if (not isinstance(chapters, list) or len(chapters) != 1
                or not isinstance(chapters[0], dict)
                or chapters[0].get("number") != active["chapter_id"]
                or not isinstance(chapters[0].get("title"), str)
                or not isinstance(chapters[0].get("paragraphs"), list)
                or not chapters[0]["paragraphs"]
                or any(not isinstance(text, str) or not text.strip()
                       for text in chapters[0]["paragraphs"])):
            raise HTTPException(502, "Completed translation has invalid chapter output")
        if dictionary_output is None:
            raise HTTPException(502, "Completed translation has no dictionary output")
        try:
            dictionary = _normalize_dictionary(dictionary_output)
        except HTTPException as exc:
            raise HTTPException(502, f"Completed translation has invalid dictionary output: {exc.detail}") from exc
        original_raw = store.read("inputs.json")["raw"]
        try:
            current_raw = _raw(book_id, active["chapter_id"])
        except HTTPException:
            current_raw = None
        if current_raw != original_raw:
            state["active"] = None
            state["last_error"] = "Saved RAW changed during translation; start translation again"
            state["last_error_chapter_id"] = active["chapter_id"]
            _save_state(book_id, state)
            return None
        result = chapters[0]
        try:
            LIBRARY.save_translated_chapter(book_id, active["chapter_id"], {
                "title": result["title"], "paragraphs": result["paragraphs"],
                "raw_hash": digest(original_raw),
                "terms_hash": dictionary_fingerprint(dictionary, original_raw),
            })
        except LNException as exc:
            raise HTTPException(500, str(exc)) from exc
        state["dictionary"] = dictionary
        state["active"] = None
        state["last_error"] = None
        state["last_error_chapter_id"] = None
        _save_state(book_id, state)
    return job


def _chapter_status(book_id: str, chapter_id: int, state: dict, job: dict | None) -> dict:
    try:
        saved = LIBRARY.load_translated_chapter(book_id, chapter_id)
    except LNException as exc:
        raise HTTPException(500, str(exc)) from exc
    stale = False
    if saved:
        try:
            raw = _raw(book_id, chapter_id)
        except HTTPException:
            stale = True
        else:
            stale = (saved.get("raw_hash") != digest(raw)
                     or saved.get("terms_hash") != dictionary_fingerprint(state["dictionary"], raw))
    active = state.get("active")
    current_job = job if active and active["chapter_id"] == chapter_id else None
    error = state.get("last_error") if state.get("last_error_chapter_id") == chapter_id else None
    status = (current_job["status"] if current_job else
              "failed" if error else
              "stale" if stale else "done" if saved else "none")
    return {
        "status": status,
        "translated": {"title": saved["title"], "paragraphs": saved["paragraphs"]} if saved else None,
        "job": current_job,
        "error": error,
        "stale": stale,
        "entry_count": len(state["dictionary"]["entries"]),
        "active_chapter_id": active["chapter_id"] if active else None,
    }


@router.get("/{book_id}/dictionary")
async def dictionary_status(book_id: str):
    with _book_lock(book_id):
        _book(book_id)
        state = _state(book_id)
        _reconcile(book_id, state)
        active = state.get("active")
        return {"entry_count": len(state["dictionary"]["entries"]),
                "imported": state.get("imported", False),
                "active_chapter_id": active["chapter_id"] if active else None}


@router.post("/{book_id}/dictionary")
async def import_dictionary(book_id: str, request: DictionaryImport):
    with _book_lock(book_id):
        _book(book_id)
        state = _state(book_id)
        _reconcile(book_id, state)
        if state.get("active"):
            raise HTTPException(409, "Finish or cancel the current chapter translation first")
        dictionary = _normalize_dictionary(request.dictionary)
        state.update(dictionary=dictionary, imported=True, last_error=None)
        state["last_error_chapter_id"] = None
        _save_state(book_id, state)
        return {"entry_count": len(dictionary["entries"])}


@router.get("/{book_id}/chapters/{chapter_id}/translation")
async def chapter_translation(book_id: str, chapter_id: int):
    with _book_lock(book_id):
        _book(book_id)
        if LIBRARY.load_chapter(book_id, chapter_id) is None:
            raise HTTPException(404, "Chapter not saved yet")
        state = _state(book_id)
        job = _reconcile(book_id, state)
        return _chapter_status(book_id, chapter_id, state, job)


@router.post("/{book_id}/chapters/{chapter_id}/translate", status_code=202)
async def translate_chapter(book_id: str, chapter_id: int, request: TranslateRequest | None = None):
    with _book_lock(book_id):
        book = _book(book_id)
        state = _state(book_id)
        try:
            job = _reconcile(book_id, state)
        except HTTPException as exc:
            active = state.get("active")
            if (exc.status_code != 502 or not (request and request.refresh)
                    or not active or active["chapter_id"] != chapter_id):
                raise
            # A completed job with missing/corrupt artifacts cannot be resumed.
            # An explicit refresh abandons it without adopting its dictionary.
            state["active"] = None
            state["last_error"] = None
            state["last_error_chapter_id"] = None
            _save_state(book_id, state)
            job = None
        active = state.get("active")
        if active:
            if active["chapter_id"] == chapter_id:
                return _chapter_status(book_id, chapter_id, state, job)
            raise HTTPException(409, f"Chapter {active['chapter_id']} is being translated")
        raw = _raw(book_id, chapter_id)
        current = _chapter_status(book_id, chapter_id, state, None)
        if not (request and request.refresh) and current["status"] == "done":
            return current
        name = f"{book_id}-chapter-{chapter_id}"
        if request and request.refresh:
            name += f"-{uuid.uuid4().hex}"
        inputs = Inputs(raw=raw, dictionary=state["dictionary"],
                        book_title=book["title"][:500], source_name=(name + ".txt")[:500])
        snapshot = await translation_api.create(inputs)
        state["active"] = {"chapter_id": chapter_id, "job_id": snapshot["job_id"]}
        state["last_error"] = None
        state["last_error_chapter_id"] = None
        _save_state(book_id, state)
        job = _reconcile(book_id, state) if snapshot["status"] == "done" else snapshot
        return _chapter_status(book_id, chapter_id, state, job)
