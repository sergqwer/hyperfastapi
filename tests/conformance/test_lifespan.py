"""Lifespan — asynccontextmanager, on_startup/on_shutdown deprecated form."""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_lifespan_runs_startup_then_shutdown_in_order() -> None:
    events: list[str] = []

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        events.append("startup")
        yield
        events.append("shutdown")

    app = FastAPI(lifespan=lifespan)

    with TestClient(app) as client:
        assert events == ["startup"]
        client.get("/")  # Force at least one request cycle
    assert events == ["startup", "shutdown"]


@pytest.mark.parametrize("expected", [["startup", "request", "shutdown"]])
def test_request_runs_between_startup_and_shutdown(expected: list[str]) -> None:
    events: list[str] = []

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        events.append("startup")
        yield
        events.append("shutdown")

    app = FastAPI(lifespan=lifespan)

    @app.get("/r")
    def r() -> dict:
        events.append("request")
        return {"ok": True}

    with TestClient(app) as client:
        client.get("/r")

    assert events == expected


def test_on_startup_deprecated_form_still_runs() -> None:
    """`@app.on_event("startup")` is deprecated but must still execute."""
    events: list[str] = []

    app = FastAPI()

    @app.on_event("startup")
    async def startup() -> None:
        events.append("startup")

    @app.on_event("shutdown")
    async def shutdown() -> None:
        events.append("shutdown")

    @app.get("/")
    def root() -> dict:
        return {}

    with TestClient(app) as client:
        client.get("/")
    assert events == ["startup", "shutdown"]


def test_lifespan_exception_in_startup_propagates() -> None:
    """If startup raises, the app must not become 'ready' — clients see 500/error."""
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        raise RuntimeError("boom")
        yield  # unreachable

    app = FastAPI(lifespan=lifespan)

    with pytest.raises(RuntimeError, match="boom"):
        with TestClient(app):
            pass
