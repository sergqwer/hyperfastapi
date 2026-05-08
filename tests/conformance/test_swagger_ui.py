"""Swagger UI — /docs endpoint."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_swagger_ui_default_endpoint() -> None:
    app = FastAPI()
    response = TestClient(app).get("/docs")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")


def test_swagger_ui_html_contains_swagger_assets() -> None:
    app = FastAPI()
    response = TestClient(app).get("/docs")
    assert "swagger-ui" in response.text.lower()


def test_swagger_ui_disabled_via_docs_url_none() -> None:
    app = FastAPI(docs_url=None)
    response = TestClient(app).get("/docs")
    assert response.status_code == 404


def test_swagger_ui_custom_url() -> None:
    app = FastAPI(docs_url="/api-docs")
    client = TestClient(app)
    assert client.get("/api-docs").status_code == 200
    # Default URL no longer works
    assert client.get("/docs").status_code == 404


def test_swagger_ui_oauth2_redirect_default() -> None:
    """The OAuth2 redirect helper is mounted by default if docs are enabled."""
    app = FastAPI()
    response = TestClient(app).get("/docs/oauth2-redirect")
    assert response.status_code == 200


def test_swagger_ui_references_openapi_url() -> None:
    """Swagger UI HTML must reference /openapi.json so it can render the spec."""
    app = FastAPI()
    response = TestClient(app).get("/docs")
    # The default swagger UI html injects the openapi url
    assert "/openapi.json" in response.text
