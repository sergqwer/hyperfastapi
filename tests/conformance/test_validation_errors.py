"""Pydantic v2 validation error format — type, loc, msg, input.

Cross-check the structure across query/path/header/body locations so a Rust
port can't subtly change the error shape.
"""

from __future__ import annotations

from typing import Annotated

from dirty_equals import IsOneOf
from fastapi import FastAPI, Header
from fastapi.testclient import TestClient
from pydantic import BaseModel

app = FastAPI()


class Item(BaseModel):
    name: str
    price: float


@app.get("/q")
def q_route(n: int) -> dict:
    return {"n": n}


@app.get("/p/{n}")
def p_route(n: int) -> dict:
    return {"n": n}


@app.get("/h")
def h_route(x_token: Annotated[str, Header()]) -> dict:
    return {"x_token": x_token}


@app.post("/b")
def b_route(item: Item) -> dict:
    return item.model_dump()


client = TestClient(app)


def test_query_missing_error_structure() -> None:
    response = client.get("/q")
    assert response.status_code == 422
    err = response.json()["detail"][0]
    assert err == {
        "type": "missing",
        "loc": ["query", "n"],
        "msg": "Field required",
        "input": IsOneOf(None, {}),
    }


def test_query_int_parsing_error_loc() -> None:
    response = client.get("/q?n=abc")
    assert response.status_code == 422
    err = response.json()["detail"][0]
    assert err["loc"] == ["query", "n"]
    assert "int" in err["type"].lower() or err["type"] == "int_parsing"


def test_path_int_parsing_error_loc() -> None:
    response = client.get("/p/abc")
    assert response.status_code == 422
    err = response.json()["detail"][0]
    assert err["loc"] == ["path", "n"]


def test_header_missing_error_loc() -> None:
    response = client.get("/h")
    assert response.status_code == 422
    err = response.json()["detail"][0]
    # FastAPI lowercases header names in the loc
    assert err["loc"][0] == "header"
    assert err["loc"][1].lower() == "x-token"


def test_body_missing_error_loc_includes_field_name() -> None:
    response = client.post("/b", json={"name": "foo"})  # price missing
    assert response.status_code == 422
    err = response.json()["detail"][0]
    assert err["type"] == "missing"
    assert err["loc"] == ["body", "price"]


def test_multiple_validation_errors_returned_as_list() -> None:
    """When multiple fields are bad, all errors come back at once (not first-fail)."""
    response = client.post("/b", json={})  # both name and price missing
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert isinstance(detail, list)
    assert len(detail) == 2
    locs = [tuple(e["loc"]) for e in detail]
    assert ("body", "name") in locs
    assert ("body", "price") in locs


def test_validation_error_response_is_json() -> None:
    response = client.get("/q")
    assert response.headers["content-type"] == "application/json"
