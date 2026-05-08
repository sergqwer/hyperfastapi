"""JSONResponse — content-type, status code, headers, dict vs Pydantic returns."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from pydantic import BaseModel


class Item(BaseModel):
    name: str
    qty: int


app = FastAPI()


@app.get("/dict")
def get_dict() -> dict:
    return {"a": 1, "b": "two"}


@app.get("/pydantic")
def get_pydantic() -> Item:
    return Item(name="apple", qty=3)


@app.get("/explicit")
def get_explicit() -> JSONResponse:
    return JSONResponse(content={"explicit": True}, status_code=201)


@app.get("/with-headers")
def get_with_headers() -> JSONResponse:
    return JSONResponse(
        content={"k": "v"},
        headers={"X-Custom": "yes", "X-Trace-Id": "abc-123"},
    )


@app.get("/list")
def get_list() -> list[int]:
    return [1, 2, 3]


client = TestClient(app)


def test_dict_return_serialized_as_json() -> None:
    response = client.get("/dict")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {"a": 1, "b": "two"}


def test_pydantic_return_serialized_as_json() -> None:
    response = client.get("/pydantic")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {"name": "apple", "qty": 3}


def test_explicit_jsonresponse_status_code() -> None:
    response = client.get("/explicit")
    assert response.status_code == 201
    assert response.json() == {"explicit": True}


def test_jsonresponse_custom_headers_preserved() -> None:
    response = client.get("/with-headers")
    assert response.status_code == 200
    assert response.headers["x-custom"] == "yes"
    assert response.headers["x-trace-id"] == "abc-123"


def test_list_top_level_serialized_as_array() -> None:
    response = client.get("/list")
    assert response.status_code == 200
    assert response.json() == [1, 2, 3]
    # Body is a JSON array, not wrapped in object
    assert response.content.startswith(b"[")


def test_content_length_matches_body_length() -> None:
    response = client.get("/dict")
    cl = response.headers.get("content-length")
    if cl is not None:
        assert int(cl) == len(response.content)
