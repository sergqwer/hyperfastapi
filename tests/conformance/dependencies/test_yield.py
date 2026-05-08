"""yield-style dependencies — cleanup runs after response, exception handling."""

from __future__ import annotations

from typing import Annotated

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

app = FastAPI()
_log: list[str] = []


def db_session():
    _log.append("db-open")
    try:
        yield "session-handle"
    finally:
        _log.append("db-close")


def maybe_fail():
    _log.append("setup")
    try:
        yield "value"
    except Exception:
        _log.append("caught-in-dep")
        raise
    finally:
        _log.append("teardown")


@app.get("/use-db")
def use_db(session: Annotated[str, Depends(db_session)]) -> dict:
    _log.append(f"handler:{session}")
    return {"session": session}


@app.get("/raise-after-yield")
def raise_after_yield(value: Annotated[str, Depends(maybe_fail)]) -> dict:
    _log.append("about-to-raise")
    raise HTTPException(status_code=500, detail="boom")


@app.get("/normal-after-yield")
def normal_after_yield(value: Annotated[str, Depends(maybe_fail)]) -> dict:
    _log.append(f"handler:{value}")
    return {"value": value}


client = TestClient(app)


def test_yield_dependency_setup_handler_teardown_order() -> None:
    _log.clear()
    response = client.get("/use-db")
    assert response.status_code == 200
    assert response.json() == {"session": "session-handle"}
    assert _log == ["db-open", "handler:session-handle", "db-close"]


def test_yield_teardown_runs_after_normal_response() -> None:
    _log.clear()
    response = client.get("/normal-after-yield")
    assert response.status_code == 200
    assert _log[-1] == "teardown"


def test_yield_teardown_runs_after_handler_raises_httpexception() -> None:
    """When the handler raises, the dep's except/finally blocks both run.

    The order is: setup → handler raises → dep's `except Exception` catches and
    re-raises → finally executes. So the log includes 'caught-in-dep' between
    the raise and the teardown.
    """
    _log.clear()
    response = client.get("/raise-after-yield")
    assert response.status_code == 500
    assert "teardown" in _log
    assert _log == ["setup", "about-to-raise", "caught-in-dep", "teardown"]


def test_yield_dependency_value_passed_to_handler() -> None:
    _log.clear()
    response = client.get("/use-db")
    assert response.json()["session"] == "session-handle"


# Multiple yield deps — cleanup in reverse order (LIFO)
mlog: list[str] = []


def first():
    mlog.append("first-setup")
    yield "first"
    mlog.append("first-teardown")


def second():
    mlog.append("second-setup")
    yield "second"
    mlog.append("second-teardown")


multi_app = FastAPI()


@multi_app.get("/multi-yield")
def multi_yield(
    a: Annotated[str, Depends(first)],
    b: Annotated[str, Depends(second)],
) -> dict:
    mlog.append("handler")
    return {"a": a, "b": b}


multi_client = TestClient(multi_app)


def test_multiple_yield_deps_teardown_in_reverse_order() -> None:
    """Cleanup follows LIFO: last setup, first teardown."""
    mlog.clear()
    response = multi_client.get("/multi-yield")
    assert response.status_code == 200
    # Setups in declaration order, teardowns in reverse
    assert mlog == [
        "first-setup",
        "second-setup",
        "handler",
        "second-teardown",
        "first-teardown",
    ]
