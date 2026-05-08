"""ReDoc — /redoc endpoint."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_redoc_default_endpoint() -> None:
    app = FastAPI()
    response = TestClient(app).get("/redoc")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")


def test_redoc_html_contains_redoc_assets() -> None:
    app = FastAPI()
    response = TestClient(app).get("/redoc")
    assert "redoc" in response.text.lower()


def test_redoc_disabled_via_redoc_url_none() -> None:
    app = FastAPI(redoc_url=None)
    response = TestClient(app).get("/redoc")
    assert response.status_code == 404


def test_redoc_custom_url() -> None:
    app = FastAPI(redoc_url="/api-redoc")
    client = TestClient(app)
    assert client.get("/api-redoc").status_code == 200
    assert client.get("/redoc").status_code == 404


def test_redoc_references_openapi_url() -> None:
    app = FastAPI()
    response = TestClient(app).get("/redoc")
    assert "/openapi.json" in response.text
