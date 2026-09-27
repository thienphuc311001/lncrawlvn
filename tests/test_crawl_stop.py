"""Stopping a crawl must keep the book protected until its writers have exited."""

import threading
import time

from fastapi.testclient import TestClient

from lncrawl import server
from lncrawl.core.taskman import TaskManager
from lncrawl.library import Library
from lncrawl.services import sources


def test_stop_waits_for_running_download_before_allowing_delete(monkeypatch, tmp_path):
    entered = threading.Event()
    release = threading.Event()
    downloaded = []

    class Crawler:
        def __init__(self):
            self.taskman = TaskManager(workers=1)

        def max_workers(self):
            return 1

        def read_novel(self, novel):
            novel.title = "Stop test book"
            novel.add_chapter(id=1, title="One", url="https://example.org/1")
            novel.add_chapter(id=2, title="Two", url="https://example.org/2")

        def format_novel(self, novel):
            pass

        def download_chapter(self, chapter):
            downloaded.append(chapter.id)
            entered.set()
            release.wait(timeout=30)
            chapter.body = "Chapter content"
            chapter.success = True

        def format_chapter(self, chapter):
            pass

        def close(self):
            self.taskman.close()

    monkeypatch.setattr(server, "LIBRARY", Library(tmp_path))
    monkeypatch.setattr(server, "JOBS", server.JobStore())
    monkeypatch.setattr(sources.Sources, "init_crawler", lambda self, url: Crawler())
    client = TestClient(server.app)
    job_id = client.post("/api/extract", json={"url": "https://example.org/book"}).json()["job_id"]
    try:
        assert entered.wait(timeout=5)
        book_path = "/api/books/" + client.get("/api/books").json()[0]["book_id"]
        assert client.get(f"{book_path}/jobs").json()[0]["job_id"] == job_id
        assert client.delete(book_path).status_code == 409
        stopped = client.post(f"/api/jobs/{job_id}/stop")
        assert stopped.status_code == 200
        assert stopped.json()["status"] == "stopping"
        assert client.delete(book_path).status_code == 409
        assert client.get(f"/api/jobs/{job_id}").json()["status"] == "stopping"
    finally:
        release.set()

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if client.get(f"/api/jobs/{job_id}").json()["status"] == "cancelled":
            break
        time.sleep(0.02)
    else:
        raise AssertionError("Crawl did not stop after the active download finished")

    assert downloaded == [1]
    assert client.get(f"{book_path}/jobs").json() == []
    assert client.delete(book_path).status_code == 204
    assert client.get(book_path).status_code == 404


def test_stop_before_worker_starts_never_runs_crawler(monkeypatch):
    monkeypatch.setattr(server, "JOBS", server.JobStore())
    job = server.JOBS.create("https://example.org/book")
    client = TestClient(server.app)
    assert client.post(f"/api/jobs/{job.job_id}/stop").json()["status"] == "stopping"
    server._run_job(job.job_id, server.ExtractRequest(url=job.url))
    assert client.get(f"/api/jobs/{job.job_id}").json()["status"] == "cancelled"
