"""Atomic checkpoints, content-addressed task cache, durable progress."""

import hashlib
import json
import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from . import prompts
from .models import MODELS, PIPELINE_VERSION, CoverageAudit, DictionaryPatch, ProposedPatch, Repair, Translation
from .notes import author_note_policy


def digest(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def pipeline_identity(inputs, policy=None):
    """One content identity for jobs and checkpoints, including semantic contracts."""
    return digest(
        {
            "inputs": inputs,
            "pipeline_version": PIPELINE_VERSION,
            "models": MODELS,
            "prompts": [prompts.DICTIONARY, prompts.TRANSLATE, prompts.REPAIR, prompts.QA],
            "policy_versions": prompts.POLICY_VERSIONS,
            "author_note_policy": author_note_policy(),
            "schemas": [
                model.model_json_schema() for model in (ProposedPatch, DictionaryPatch, Translation, Repair, CoverageAudit)
            ],
        }
    )


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_text(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _sync_directory(path):
    """Make a completed checkpoint rename durable across a power loss."""
    if not hasattr(os, "O_DIRECTORY"):
        return
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class Store:
    def __init__(self, root: Path, inputs=None, job_id=None):
        if job_id is None and inputs is not None:
            current_id = pipeline_identity(inputs)
            if (root / current_id).is_dir():
                job_id = current_id
            else:
                job_id = self._find_legacy_job(root, inputs) or current_id
        self.id = job_id
        self.path = root / self.id
        if inputs is not None:
            existing = self.path / "inputs.json"
            if not existing.exists():
                atomic_json(existing, inputs)
            elif self.id == pipeline_identity(inputs):
                if json.loads(existing.read_text(encoding="utf-8")) != inputs:
                    raise ValueError("Translation job identity collides with different inputs")
        if not self.path.is_dir():
            raise FileNotFoundError("Translation job not found")

    @staticmethod
    def _find_legacy_job(root, inputs):
        """Migrate only unversioned pre-rebuild jobs with exact RAW/dictionary inputs.

        A versioned RAW-only job belongs to its original policy identity and must
        never be silently reused after a prompt or validator change.
        """
        if author_note_policy() != "preserve":
            return None
        if not root.is_dir():
            return None
        matches = []
        for path in root.glob("*/inputs.json"):
            if not re.fullmatch(r"[a-f0-9]{64}", path.parent.name):
                continue
            try:
                old = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(old, dict):
                continue
            # Modern API jobs record the parser version on creation, even if the
            # translation later fails before writing a final pipeline version.
            if ((path.parent / "pipeline-version.json").exists()
                    or (path.parent / "parser-version.json").exists()):
                continue
            if old.get("raw") != inputs.get("raw") or old.get("dictionary") != inputs.get("dictionary"):
                continue
            safe_legacy = True
            for chapter in (path.parent / "chapters").glob("*.json"):
                try:
                    if "pipeline_version" in json.loads(chapter.read_text(encoding="utf-8")):
                        safe_legacy = False
                        break
                except (OSError, ValueError):
                    safe_legacy = False
                    break
            if not safe_legacy:
                continue
            matches.append((path.stat().st_mtime, path.parent.name))
        return max(matches)[1] if matches else None

    def read(self, name, default=None):
        path = self.path / name
        if not path.exists():
            return default
        if path.suffix == ".txt":
            return path.read_text(encoding="utf-8")
        return json.loads(path.read_text(encoding="utf-8"))

    def write(self, name, value):
        path = self.path / name
        if path.suffix == ".txt" and isinstance(value, str):
            atomic_text(path, value)
        else:
            atomic_json(path, value)

    def progress(self, status="running", **fields):
        value = self.read("progress.json", {})
        changed = (value.get("status"), value.get("stage")) != (
            status,
            fields.get("stage", value.get("stage")),
        )
        value.update(job_id=self.id, status=status, **fields)
        self.write("progress.json", value)
        if changed:
            self.log(
                {
                    "status": status,
                    "task": "batch",
                    "message": value.get("stage", status),
                    **({"error": value["error"]} if value.get("error") else {}),
                }
            )
        return value

    def diagnostic(self, metadata):
        with (self.path / "requests.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(metadata, ensure_ascii=False) + "\n")
        self.log(metadata)

    def request_statistics(self):
        stats = self.read("request-statistics.json", {}) or {}
        stats.setdefault("by_operation", {})
        stats.setdefault("logical_call_count", 0)
        stats.setdefault("api_attempt_count", 0)
        stats.setdefault("technical_retry_count", 0)
        stats.setdefault("fallback_count", 0)
        stats.setdefault("cache_hit_count", 0)
        # Compatibility fields remain available to existing UI/saved jobs.
        stats.setdefault("logical_operations", {})
        stats.setdefault("requests", {})
        stats.setdefault("local_ai_requests", {})
        stats.setdefault("total_requests", stats["api_attempt_count"])
        stats.setdefault("retry", stats["technical_retry_count"])
        stats.setdefault("model_fallback", stats["fallback_count"])
        stats.setdefault("cache_hits", stats["cache_hit_count"])
        return stats

    def account(self, operation, event):
        stats = self.request_statistics()
        bucket = stats["by_operation"].setdefault(operation, {
            "logical_calls": 0,
            "api_attempts": 0,
            "technical_retries": 0,
            "fallbacks": 0,
            "cache_hits": 0,
        })
        if event == "logical":
            bucket["logical_calls"] += 1
            stats["logical_call_count"] += 1
            stats["logical_operations"][operation] = stats["logical_operations"].get(operation, 0) + 1
        elif event == "attempt":
            bucket["api_attempts"] += 1
            stats["api_attempt_count"] += 1
            stats["requests"][operation] = stats["requests"].get(operation, 0) + 1
            stats["total_requests"] += 1
        elif event == "technical_retry":
            bucket["technical_retries"] += 1
            stats["technical_retry_count"] += 1
            stats["retry"] += 1
        elif event == "fallback":
            bucket["fallbacks"] += 1
            stats["fallback_count"] += 1
            stats["model_fallback"] += 1
        elif event == "cache_hit":
            bucket["cache_hits"] += 1
            stats["cache_hit_count"] += 1
            stats["cache_hits"] += 1
        self.write("request-statistics.json", stats)

    def chapter_account(self, chapter, operation, event, model=None):
        if chapter is None:
            return
        name = f"chapter-request-statistics/{chapter}.json"
        stats = self.read(name, {}) or {}
        bucket = stats.setdefault(operation, {
            "logical_calls": 0, "api_attempts": 0,
            "technical_retries": 0, "fallbacks": 0,
            "cache_hits": 0, "actual_models": [],
        })
        field = {
            "logical": "logical_calls",
            "attempt": "api_attempts",
            "technical_retry": "technical_retries",
            "fallback": "fallbacks",
            "cache_hit": "cache_hits",
        }.get(event)
        if field:
            bucket[field] += 1
        if model and model not in bucket["actual_models"]:
            bucket["actual_models"].append(model)
        self.write(name, stats)

    def cache_get(self, fingerprint):
        value = self.read(f"cache/{fingerprint}.json")
        if not isinstance(value, dict) or value.get("fingerprint") != fingerprint:
            return None
        return value

    def cache_put(self, fingerprint, value, metadata):
        self.write(f"cache/{fingerprint}.json", {
            "fingerprint": fingerprint,
            "value": value,
            "metadata": metadata,
        })

    def log(self, metadata):
        event = {
            **metadata,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "id": uuid.uuid4().hex,
        }
        with (self.path / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")

    def logs(self, limit=100):
        """Read a bounded tail so polling never loads an entire long-running log."""
        path = self.path / "events.jsonl"
        if not path.exists():
            path = self.path / "requests.jsonl"  # Existing jobs retain their request history.
        if not path.exists():
            return []
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - 131072))
            if size > 131072:
                stream.readline()  # Discard a partial UTF-8/JSON line.
            lines = stream.read().splitlines()[-limit:]
        result = []
        for line in lines:
            try:
                result.append(json.loads(line))
            except (ValueError, UnicodeDecodeError):
                pass  # A writer may still be appending the final event.
        return result

    @contextmanager
    def execution(self):
        """Cross-process ownership; SQLite releases the lease even after a crash."""
        connection = sqlite3.connect(self.path / "execution.sqlite3", timeout=0)
        try:
            try:
                connection.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                if "locked" in str(exc):
                    raise AlreadyRunning("This translation batch is already running") from exc
                raise
            yield
        finally:
            connection.rollback()
            connection.close()

    def is_active(self):
        if not (self.path / "execution.sqlite3").exists():
            return False
        try:
            with self.execution():
                return False
        except AlreadyRunning:
            return True


class AlreadyRunning(RuntimeError):
    pass
