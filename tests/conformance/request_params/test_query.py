"""Query parameters: required/optional, typed, list, validators, alias."""

from __future__ import annotations

from typing import Annotated

import pytest
from fastapi import FastAPI, Query
from fastapi.testclient import TestClient

app = FastAPI()


@app.get("/required-str")
def get_required_str(q: str) -> dict:
    return {"q": q}


@app.get("/optional-str")
def get_optional_str(q: str | None = None) -> dict:
    return {"q": q}


@app.get("/typed-int")
def get_typed_int(n: int) -> dict:
    return {"n": n}


@app.get("/typed-float")
def get_typed_float(f: float) -> dict:
    return {"f": f}


@app.get("/typed-bool")
def get_typed_bool(b: bool) -> dict:
    return {"b": b}


@app.get("/list-q")
def get_list_q(q: Annotated[list[str], Query()] = []) -> dict:
    return {"q": q}


@app.get("/aliased")
def get_aliased(my_q: Annotated[str, Query(alias="other-name")]) -> dict:
    return {"my_q": my_q}


@app.get("/validated")
def get_validated(
    n: Annotated[int, Query(gt=0, le=100)],
    s: Annotated[str, Query(min_length=2, max_length=10)],
) -> dict:
    return {"n": n, "s": s}


client = TestClient(app)


def test_required_query_present() -> None:
    response = client.get("/required-str?q=hello")
    assert response.status_code == 200, response.text
    assert response.json() == {"q": "hello"}


def test_required_query_missing_returns_422() -> None:
    response = client.get("/required-str")
    assert response.status_code == 422
    body = response.json()
    assert body["detail"][0]["type"] == "missing"
    assert body["detail"][0]["loc"] == ["query", "q"]


def test_optional_query_absent_is_none() -> None:
    response = client.get("/optional-str")
    assert response.status_code == 200
    assert response.json() == {"q": None}


def test_optional_query_present() -> None:
    response = client.get("/optional-str?q=foo")
    assert response.status_code == 200
    assert response.json() == {"q": "foo"}


def test_typed_int_parses() -> None:
    response = client.get("/typed-int?n=42")
    assert response.status_code == 200
    assert response.json() == {"n": 42}


def test_typed_int_rejects_non_numeric() -> None:
    response = client.get("/typed-int?n=abc")
    assert response.status_code == 422
    err = response.json()["detail"][0]
    assert err["loc"] == ["query", "n"]
    assert "int" in err["type"].lower()


def test_typed_bool_parses_true() -> None:
    response = client.get("/typed-bool?b=true")
    assert response.status_code == 200
    assert response.json() == {"b": True}


def test_typed_bool_parses_false() -> None:
    response = client.get("/typed-bool?b=false")
    assert response.status_code == 200
    assert response.json() == {"b": False}


def test_list_query_multiple_values() -> None:
    response = client.get("/list-q?q=a&q=b&q=c")
    assert response.status_code == 200
    assert response.json() == {"q": ["a", "b", "c"]}


def test_alias_uses_external_name() -> None:
    response = client.get("/aliased?other-name=hello")
    assert response.status_code == 200
    assert response.json() == {"my_q": "hello"}


def test_alias_python_name_does_not_work() -> None:
    """When alias is set, the Python parameter name is NOT accepted."""
    response = client.get("/aliased?my_q=hello")
    assert response.status_code == 422


def test_validator_gt_rejects_zero() -> None:
    response = client.get("/validated?n=0&s=hello")
    assert response.status_code == 422
    err = response.json()["detail"][0]
    assert err["loc"] == ["query", "n"]


def test_validator_le_rejects_above() -> None:
    response = client.get("/validated?n=101&s=hello")
    assert response.status_code == 422


def test_validator_min_length_rejects_short() -> None:
    response = client.get("/validated?n=5&s=a")
    assert response.status_code == 422
    err = response.json()["detail"][0]
    assert err["loc"] == ["query", "s"]


def test_validator_passes_with_valid_values() -> None:
    response = client.get("/validated?n=50&s=ok")
    assert response.status_code == 200
    assert response.json() == {"n": 50, "s": "ok"}
