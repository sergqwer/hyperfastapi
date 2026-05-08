"""Form parameters: urlencoded body, multiple values, validation errors."""

from __future__ import annotations

from typing import Annotated

from fastapi import FastAPI, Form
from fastapi.testclient import TestClient

app = FastAPI()


@app.post("/login")
def login(username: Annotated[str, Form()], password: Annotated[str, Form()]) -> dict:
    return {"username": username, "password_len": len(password)}


@app.post("/optional")
def with_optional(
    username: Annotated[str, Form()],
    nickname: Annotated[str | None, Form()] = None,
) -> dict:
    return {"username": username, "nickname": nickname}


@app.post("/list")
def list_form(tags: Annotated[list[str], Form()] = []) -> dict:
    return {"tags": tags}


@app.post("/typed")
def typed_form(
    name: Annotated[str, Form()],
    age: Annotated[int, Form()],
    active: Annotated[bool, Form()],
) -> dict:
    return {"name": name, "age": age, "active": active}


client = TestClient(app)


def test_form_urlencoded_happy_path() -> None:
    response = client.post(
        "/login",
        data={"username": "alice", "password": "secret123"},
    )
    assert response.status_code == 200
    assert response.json() == {"username": "alice", "password_len": 9}


def test_form_missing_required_returns_422() -> None:
    response = client.post("/login", data={"username": "alice"})
    assert response.status_code == 422
    err = response.json()["detail"][0]
    assert err["loc"] == ["body", "password"]
    assert err["type"] == "missing"


def test_form_optional_field_absent() -> None:
    response = client.post("/optional", data={"username": "bob"})
    assert response.status_code == 200
    assert response.json() == {"username": "bob", "nickname": None}


def test_form_list_multiple_values() -> None:
    response = client.post("/list", data={"tags": ["a", "b"]})
    assert response.status_code == 200
    assert response.json() == {"tags": ["a", "b"]}


def test_form_typed_int_parses() -> None:
    response = client.post("/typed", data={"name": "x", "age": "42", "active": "true"})
    assert response.status_code == 200
    assert response.json() == {"name": "x", "age": 42, "active": True}


def test_form_typed_int_invalid_returns_422() -> None:
    response = client.post(
        "/typed", data={"name": "x", "age": "abc", "active": "true"}
    )
    assert response.status_code == 422
    err = response.json()["detail"][0]
    assert err["loc"] == ["body", "age"]


def test_form_content_type_must_be_urlencoded_or_multipart() -> None:
    """Sending JSON body to a form route should be a 422 (no form data parsed)."""
    response = client.post(
        "/login",
        json={"username": "alice", "password": "secret"},
    )
    assert response.status_code == 422


def test_form_empty_string_value() -> None:
    response = client.post(
        "/login", data={"username": "", "password": ""}
    )
    assert response.status_code == 200
    assert response.json() == {"username": "", "password_len": 0}
