"""FileResponse — serving files from disk, content-type detection, filename."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def text_file() -> Path:
    """Create a temp text file with known content for the FileResponse tests."""
    tmp = tempfile.NamedTemporaryFile("wb", suffix=".txt", delete=False)
    tmp.write(b"file contents here")
    tmp.close()
    yield Path(tmp.name)
    Path(tmp.name).unlink(missing_ok=True)


@pytest.fixture(scope="module")
def binary_file() -> Path:
    tmp = tempfile.NamedTemporaryFile("wb", suffix=".bin", delete=False)
    tmp.write(bytes(range(256)))
    tmp.close()
    yield Path(tmp.name)
    Path(tmp.name).unlink(missing_ok=True)


def make_app(file_path: Path, **kwargs) -> FastAPI:
    app = FastAPI()

    @app.get("/file")
    def get_file() -> FileResponse:
        return FileResponse(path=str(file_path), **kwargs)

    return app


def test_file_response_serves_file_contents(text_file: Path) -> None:
    client = TestClient(make_app(text_file))
    response = client.get("/file")
    assert response.status_code == 200
    assert response.content == b"file contents here"


def test_file_response_content_type_inferred(text_file: Path) -> None:
    """`.txt` → text/plain (mimetype lookup)."""
    client = TestClient(make_app(text_file))
    response = client.get("/file")
    ct = response.headers["content-type"]
    assert ct.startswith("text/plain") or ct.startswith("text/")


def test_file_response_explicit_filename_in_disposition(text_file: Path) -> None:
    """`filename=...` adds Content-Disposition: attachment with that name."""
    client = TestClient(make_app(text_file, filename="download.txt"))
    response = client.get("/file")
    assert "download.txt" in response.headers.get("content-disposition", "")


def test_file_response_explicit_media_type(text_file: Path) -> None:
    """Explicit `media_type` overrides the mimetype guess."""
    client = TestClient(make_app(text_file, media_type="application/custom"))
    response = client.get("/file")
    assert response.headers["content-type"] == "application/custom"


def test_file_response_binary_passthrough(binary_file: Path) -> None:
    client = TestClient(make_app(binary_file))
    response = client.get("/file")
    assert response.status_code == 200
    assert response.content == bytes(range(256))


def test_file_response_content_length_matches(text_file: Path) -> None:
    client = TestClient(make_app(text_file))
    response = client.get("/file")
    cl = response.headers.get("content-length")
    if cl is not None:
        assert int(cl) == len(response.content)
