"""Middleware — CORS, GZip, custom @app.middleware, ordering."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import Response
from fastapi.testclient import TestClient


# --- CORS ---
cors_app = FastAPI()
cors_app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://example.com"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@cors_app.get("/data")
def cors_data() -> dict:
    return {"ok": True}


# --- GZip ---
gzip_app = FastAPI()
gzip_app.add_middleware(GZipMiddleware, minimum_size=10)


@gzip_app.get("/big")
def big_payload() -> dict:
    return {"data": "x" * 1000}


# --- Custom @middleware decorator ---
custom_app = FastAPI()
_order: list[str] = []


@custom_app.middleware("http")
async def add_header(request: Request, call_next):
    _order.append("middleware-pre")
    response: Response = await call_next(request)
    _order.append("middleware-post")
    response.headers["X-Processed"] = "yes"
    return response


@custom_app.get("/wrapped")
def wrapped() -> dict:
    _order.append("handler")
    return {"ok": True}


cors_client = TestClient(cors_app)
gzip_client = TestClient(gzip_app)
custom_client = TestClient(custom_app)


def test_cors_preflight_returns_allow_origin() -> None:
    response = cors_client.options(
        "/data",
        headers={
            "Origin": "https://example.com",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "https://example.com"


def test_cors_actual_request_includes_origin_header() -> None:
    response = cors_client.get("/data", headers={"Origin": "https://example.com"})
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "https://example.com"


def test_cors_disallowed_origin_omits_allow_origin() -> None:
    response = cors_client.get("/data", headers={"Origin": "https://evil.com"})
    assert response.status_code == 200
    # Disallowed origins simply don't get the allow-origin header
    assert response.headers.get("access-control-allow-origin") != "https://evil.com"


def test_gzip_compresses_when_client_accepts() -> None:
    response = gzip_client.get("/big", headers={"Accept-Encoding": "gzip"})
    assert response.status_code == 200
    assert response.headers.get("content-encoding") == "gzip"
    # httpx auto-decompresses; check the decoded body
    assert response.json() == {"data": "x" * 1000}


def test_gzip_skips_when_client_does_not_accept() -> None:
    response = gzip_client.get("/big", headers={"Accept-Encoding": "identity"})
    assert response.status_code == 200
    assert "gzip" not in (response.headers.get("content-encoding") or "")


def test_custom_middleware_wraps_handler_in_order() -> None:
    """Middleware code runs before AND after the handler."""
    _order.clear()
    response = custom_client.get("/wrapped")
    assert response.status_code == 200
    assert response.headers["x-processed"] == "yes"
    # Order: middleware pre → handler → middleware post
    assert _order == ["middleware-pre", "handler", "middleware-post"]
