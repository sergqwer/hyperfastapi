"""StaticFiles — mount a directory as a static asset server."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def static_dir() -> Path:
    """Create a temp directory with sample files for static serving."""
    tmp = Path(tempfile.mkdtemp(prefix="fastapi-static-"))
    # Use write_bytes to avoid Windows newline translation (\n → \r\n).
    (tmp / "hello.txt").write_bytes(b"hello from static\n")
    (tmp / "data.json").write_bytes(b'{"static": true}')
    sub = tmp / "sub"
    sub.mkdir()
    (sub / "nested.txt").write_bytes(b"nested content")
    yield tmp
    # Cleanup
    import shutil

    shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture(scope="module")
def app(static_dir: Path) -> FastAPI:
    a = FastAPI()
    a.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @a.get("/")
    def root() -> dict:
        return {"app": "ok"}

    return a


def test_static_file_served(app: FastAPI) -> None:
    response = TestClient(app).get("/static/hello.txt")
    assert response.status_code == 200
    assert response.text == "hello from static\n"


def test_static_json_file_served_as_text(app: FastAPI) -> None:
    """StaticFiles serves files as-is; .json file is served with json content-type."""
    response = TestClient(app).get("/static/data.json")
    assert response.status_code == 200
    assert response.json() == {"static": True}


def test_static_unknown_file_404(app: FastAPI) -> None:
    response = TestClient(app).get("/static/nope.txt")
    assert response.status_code == 404


def test_static_nested_file_served(app: FastAPI) -> None:
    response = TestClient(app).get("/static/sub/nested.txt")
    assert response.status_code == 200
    assert response.text == "nested content"


def test_app_routes_still_work_alongside_static(app: FastAPI) -> None:
    """Mounting StaticFiles must not break regular API routes."""
    response = TestClient(app).get("/")
    assert response.status_code == 200
    assert response.json() == {"app": "ok"}
