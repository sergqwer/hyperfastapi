"""response_class / default_response_class — at FastAPI/APIRouter/route level."""

from __future__ import annotations

from fastapi import APIRouter, FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.testclient import TestClient


def test_default_response_class_on_app() -> None:
    """default_response_class on FastAPI affects routes that return non-Response objects."""
    app = FastAPI(default_response_class=HTMLResponse)

    @app.get("/page")
    def page() -> str:
        return "<h1>Hi</h1>"

    response = TestClient(app).get("/page")
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert response.text == "<h1>Hi</h1>"


def test_default_response_class_on_router() -> None:
    """default_response_class on APIRouter applies to its routes."""
    app = FastAPI()
    router = APIRouter(default_response_class=PlainTextResponse)

    @router.get("/text")
    def text() -> str:
        return "plain"

    app.include_router(router)

    response = TestClient(app).get("/text")
    assert response.headers["content-type"] == "text/plain; charset=utf-8"


def test_route_response_class_overrides_app_default() -> None:
    """response_class on a specific route wins over app-level default."""
    app = FastAPI(default_response_class=HTMLResponse)

    @app.get("/json", response_class=JSONResponse)
    def json_route() -> dict:
        return {"k": "v"}

    response = TestClient(app).get("/json")
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {"k": "v"}


def test_explicit_response_object_wins_over_response_class() -> None:
    """Returning a Response instance bypasses response_class entirely."""
    app = FastAPI(default_response_class=HTMLResponse)

    @app.get("/forced")
    def forced() -> JSONResponse:
        return JSONResponse(content={"explicit": True})

    response = TestClient(app).get("/forced")
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {"explicit": True}


def test_app_jsonresponse_default_unchanged() -> None:
    """Without any setting, JSONResponse is the implicit default."""
    app = FastAPI()

    @app.get("/")
    def root() -> dict:
        return {"x": 1}

    response = TestClient(app).get("/")
    assert response.headers["content-type"] == "application/json"
