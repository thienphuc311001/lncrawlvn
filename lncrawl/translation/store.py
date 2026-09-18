"""Atomic checkpoints, content-addressed task cache, durable progress."""

import hashlib
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from . import prompts
from .models import (
    MODELS,
    PIPELINE_VERSION,
    BatchResolution,
    Repair,
    ResolutionPolicy,
    Term,
    Translation,
)


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
            "resolution_policy": policy or ResolutionPolicy.from_environment().model_dump(),
            "prompts": [prompts.BATCH_RESOLVE, prompts.TRANSLATE, prompts.REPAIR],
            "schemas": [
                model.model_json_schema() for model in (Term, BatchResolution, Translation, Repair)
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
    finally:
        temporary.unlink(missing_ok=True)


class Store:
    def __init__(self, root: Path, inputs=None, job_id=None):
        self.policy = ResolutionPolicy.from_environment()
        self.id = job_id or pipeline_identity(inputs, self.policy.model_dump())
        self.path = root / self.id
        if inputs is not None:
            atomic_json(self.path / "inputs.json", inputs)
            atomic_json(self.path / "resolution-policy.json", self.policy.model_dump())
        if not self.path.is_dir():
            raise FileNotFoundError("Translation job not found")

    def read(self, name, default=None):
        path = self.path / name
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default

    def write(self, name, value):
        atomic_json(self.path / name, value)

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
        return self.read(
            "request-statistics.json",
            {
                "logical_operations": {
                    name: 0
                    for name in (
                        "terminology_resolver",
                        "semantic_dictionary_conflict",
                        "translation",
                        "repair",
                    )
                },
                "requests": {
                    name: 0
                    for name in (
                        "terminology_resolver",
                        "semantic_dictionary_conflict",
                        "translation",
                        "repair",
                    )
                },
                "local_ai_requests": {
                    name: 0
                    for name in (
                        "alignment",
                        "scanning",
                        "occurrence_aggregation",
                        "evidence_selection",
                        "structural_dictionary_audit",
                        "chunk_construction",
                        "normal_output_validation",
                    )
                },
                "total_requests": 0,
                "retry": 0,
                "model_fallback": 0,
                "cache_hits": 0,
            },
        )

    def account(self, operation, event, model_fallback=False):
        stats = self.request_statistics()
        if event == "logical":
            stats["logical_operations"][operation] += 1
        elif event == "running":
            stats["requests"][operation] += 1
            stats["total_requests"] += 1
        elif event in ("retry", "cache_hits"):
            stats[event] += 1
        if model_fallback:
            stats["model_fallback"] += 1
        self.write("request-statistics.json", stats)

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
