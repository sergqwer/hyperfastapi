"""Cookie parameters: required, optional, multiple cookies."""

from __future__ import annotations

from typing import Annotated

from fastapi import Cookie, FastAPI
from fastapi.testclient import TestClient

app = FastAPI()


@app.get("/required")
def required_cookie(session_id: Annotated[str, Cookie()]) -> dict:
    return {"session_id": session_id}


@app.get("/optional")
def optional_cookie(session_id: Annotated[str | None, Cookie()] = None) -> dict:
    return {"session_id": session_id}


@app.get("/multi")
def multi_cookie(
    a: Annotated[str | None, Cookie()] = None,
    b: Annotated[str | None, Cookie()] = None,
) -> dict:
    return {"a": a, "b": b}


client = TestClient(app)


def test_cookie_required_present() -> None:
    response = client.get("/required", cookies={"session_id": "abc123"})
    assert response.status_code == 200
    assert response.json() == {"session_id": "abc123"}


def test_cookie_required_missing_returns_422() -> None:
    response = client.get("/required")
    assert response.status_code == 422
    err = response.json()["detail"][0]
    assert err["loc"] == ["cookie", "session_id"]


def test_cookie_optional_missing_is_none() -> None:
    response = client.get("/optional")
    assert response.status_code == 200
    assert response.json() == {"session_id": None}


def test_cookie_optional_present() -> None:
    response = client.get("/optional", cookies={"session_id": "xyz"})
    assert response.status_code == 200
    assert response.json() == {"session_id": "xyz"}


def test_multiple_cookies() -> None:
    response = client.get("/multi", cookies={"a": "alpha", "b": "beta"})
    assert response.status_code == 200
    assert response.json() == {"a": "alpha", "b": "beta"}


def test_cookie_one_missing() -> None:
    response = client.get("/multi", cookies={"a": "alpha"})
    assert response.status_code == 200
    assert response.json() == {"a": "alpha", "b": None}
