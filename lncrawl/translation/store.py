"""Atomic checkpoints, content-addressed task cache, durable progress."""

import hashlib
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path

from .models import MODELS, PIPELINE_VERSION


def digest(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


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
        self.id = job_id or digest(
            {"inputs": inputs, "version": PIPELINE_VERSION, "models": MODELS}
        )
        self.path = root / self.id
        if inputs is not None:
            atomic_json(self.path / "inputs.json", inputs)
        if not self.path.is_dir():
            raise FileNotFoundError("Translation job not found")

    def read(self, name, default=None):
        path = self.path / name
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default

    def write(self, name, value):
        atomic_json(self.path / name, value)

    def progress(self, status="running", **fields):
        value = self.read("progress.json", {})
        value.update(job_id=self.id, status=status, **fields)
        self.write("progress.json", value)
        return value

    def diagnostic(self, metadata):
        with (self.path / "requests.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(metadata, ensure_ascii=False) + "\n")

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
