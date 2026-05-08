"""Jinja2Templates — server-side template rendering."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def templates_dir() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="fastapi-templates-"))
    (tmp / "hello.html").write_text(
        "<h1>Hello, {{ name }}!</h1><p>Items: {{ items|length }}</p>"
    )
    (tmp / "missing-var.html").write_text("<p>{{ missing }}</p>")
    yield tmp
    import shutil

    shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture(scope="module")
def app(templates_dir: Path) -> FastAPI:
    templates = Jinja2Templates(directory=str(templates_dir))
    a = FastAPI()

    @a.get("/hello", response_class=HTMLResponse)
    def hello(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="hello.html",
            context={"name": "World", "items": [1, 2, 3]},
        )

    @a.get("/missing-var", response_class=HTMLResponse)
    def missing_var(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request, name="missing-var.html", context={}
        )

    return a


def test_template_renders_with_context(app: FastAPI) -> None:
    response = TestClient(app).get("/hello")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Hello, World!" in response.text
    assert "Items: 3" in response.text


def test_template_missing_variable_renders_empty_string(app: FastAPI) -> None:
    """Jinja2 default: undefined variable → empty string in output."""
    response = TestClient(app).get("/missing-var")
    assert response.status_code == 200
    assert response.text == "<p></p>"


def test_template_response_content_type(app: FastAPI) -> None:
    response = TestClient(app).get("/hello")
    assert response.headers["content-type"] == "text/html; charset=utf-8"


def test_template_unicode_context(templates_dir: Path) -> None:
    """Non-ASCII variables render correctly through the UTF-8 pipeline."""
    templates = Jinja2Templates(directory=str(templates_dir))
    app2 = FastAPI()

    @app2.get("/u", response_class=HTMLResponse)
    def u(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request, name="hello.html", context={"name": "Світ", "items": []}
        )

    response = TestClient(app2).get("/u")
    assert response.status_code == 200
    assert "Світ" in response.text


def test_template_nonexistent_raises(app: FastAPI) -> None:
    """Asking for a non-existent template should raise (Jinja TemplateNotFound)."""
    templates = Jinja2Templates(directory="/nonexistent-base-dir-xyz")
    bad_app = FastAPI()

    @bad_app.get("/x", response_class=HTMLResponse)
    def x(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request, name="does-not-exist.html", context={}
        )

    with pytest.raises(Exception):
        TestClient(bad_app).get("/x")
