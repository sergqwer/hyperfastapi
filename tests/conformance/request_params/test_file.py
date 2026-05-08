"""File upload: UploadFile, bytes, multiple files, content_type, filename."""

from __future__ import annotations

from typing import Annotated

from fastapi import FastAPI, File, UploadFile
from fastapi.testclient import TestClient

app = FastAPI()


@app.post("/bytes")
def upload_bytes(file: Annotated[bytes, File()]) -> dict:
    return {"size": len(file), "preview": file[:10].decode("utf-8", errors="replace")}


@app.post("/upload")
def upload_file(file: UploadFile) -> dict:
    return {
        "filename": file.filename,
        "content_type": file.content_type,
        "size": file.size,
    }


@app.post("/multi")
def upload_multi(files: list[UploadFile]) -> dict:
    return {
        "count": len(files),
        "names": [f.filename for f in files],
    }


@app.post("/optional")
def upload_optional(file: UploadFile | None = None) -> dict:
    if file is None:
        return {"file": None}
    return {"filename": file.filename}


client = TestClient(app)


def test_upload_bytes_received() -> None:
    response = client.post("/bytes", files={"file": ("data.txt", b"hello world")})
    assert response.status_code == 200
    assert response.json() == {"size": 11, "preview": "hello worl"}


def test_upload_file_metadata() -> None:
    response = client.post(
        "/upload",
        files={"file": ("greeting.txt", b"Hello!", "text/plain")},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["filename"] == "greeting.txt"
    assert body["content_type"] == "text/plain"
    assert body["size"] == 6


def test_upload_file_default_content_type() -> None:
    """Without explicit content-type, multipart parser uses 'application/octet-stream'."""
    response = client.post("/upload", files={"file": ("x.bin", b"\x00\x01\x02")})
    assert response.status_code == 200
    # Default content-type for binary data
    assert response.json()["content_type"] in ("application/octet-stream", "text/plain")


def test_upload_multiple_files() -> None:
    response = client.post(
        "/multi",
        files=[
            ("files", ("a.txt", b"A")),
            ("files", ("b.txt", b"B")),
            ("files", ("c.txt", b"C")),
        ],
    )
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 3
    assert body["names"] == ["a.txt", "b.txt", "c.txt"]


def test_upload_required_missing_returns_422() -> None:
    response = client.post("/upload")
    assert response.status_code == 422


def test_upload_optional_absent() -> None:
    response = client.post("/optional")
    assert response.status_code == 200
    assert response.json() == {"file": None}


def test_upload_optional_present() -> None:
    response = client.post(
        "/optional", files={"file": ("hi.txt", b"hi")}
    )
    assert response.status_code == 200
    assert response.json() == {"filename": "hi.txt"}


def test_upload_large_bytes() -> None:
    """Sanity: medium-sized payload (1MB) goes through without truncation."""
    payload = b"x" * (1024 * 1024)
    response = client.post("/bytes", files={"file": ("big.bin", payload)})
    assert response.status_code == 200
    assert response.json()["size"] == 1024 * 1024
