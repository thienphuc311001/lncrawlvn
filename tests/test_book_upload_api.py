"""Upload request limits must reject a batch before any book is saved."""

from fastapi.testclient import TestClient

from lncrawl import server
from lncrawl.library import Library


def test_combined_upload_limit_rejects_batch_without_partial_import(monkeypatch, tmp_path):
    library = Library(tmp_path / "library")
    monkeypatch.setattr(server, "LIBRARY", library)
    monkeypatch.setattr(server, "MAX_UPLOAD_BYTES", 5)
    client = TestClient(server.app)

    response = client.post("/api/books/upload", files=[
        ("files", ("one.txt", b"abc", "text/plain")),
        ("files", ("two.txt", b"def", "text/plain")),
    ])

    assert response.status_code == 413
    assert library.list_books() == []
    assert not library.root.exists()

    accepted = client.post("/api/books/upload", files=[
        ("files", ("one.txt", b"abcde", "text/plain")),
    ])
    assert accepted.status_code == 200
    result = accepted.json()
    assert result["errors"] == []
    assert result["books"][0]["title"] == "one"
    book_id = result["books"][0]["book_id"]
    assert "abcde" in library.load_chapter(book_id, 1)["body"]
