"""Path parameters: typed, validators, path:path catchall, required-by-design."""

from __future__ import annotations

from typing import Annotated

import pytest
from fastapi import FastAPI, Path
from fastapi.testclient import TestClient

app = FastAPI()


@app.get("/items/{item_id}")
def get_item(item_id: int) -> dict:
    return {"item_id": item_id}


@app.get("/users/{name}")
def get_user(name: str) -> dict:
    return {"name": name}


@app.get("/validated/{n}")
def get_validated(n: Annotated[int, Path(gt=0, le=1000)]) -> dict:
    return {"n": n}


@app.get("/files/{file_path:path}")
def get_file(file_path: str) -> dict:
    return {"file_path": file_path}


client = TestClient(app)


def test_path_int_parses() -> None:
    response = client.get("/items/42")
    assert response.status_code == 200
    assert response.json() == {"item_id": 42}


def test_path_int_rejects_string() -> None:
    response = client.get("/items/foo")
    assert response.status_code == 422
    err = response.json()["detail"][0]
    assert err["loc"] == ["path", "item_id"]


def test_path_string_accepts_anything() -> None:
    response = client.get("/users/alice")
    assert response.status_code == 200
    assert response.json() == {"name": "alice"}


def test_path_validator_gt_rejects_zero() -> None:
    response = client.get("/validated/0")
    assert response.status_code == 422


def test_path_validator_le_rejects_above() -> None:
    response = client.get("/validated/1001")
    assert response.status_code == 422


def test_path_validator_accepts_valid() -> None:
    response = client.get("/validated/500")
    assert response.status_code == 200
    assert response.json() == {"n": 500}


def test_path_path_catchall_includes_slashes() -> None:
    """`{file_path:path}` must capture slash-containing strings."""
    response = client.get("/files/static/css/main.css")
    assert response.status_code == 200
    assert response.json() == {"file_path": "static/css/main.css"}
