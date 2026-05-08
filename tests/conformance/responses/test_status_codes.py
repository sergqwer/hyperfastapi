"""Status codes — explicit values, 200/201/202/204/206/3xx/4xx/5xx behaviors."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI, status
from fastapi.responses import Response
from fastapi.testclient import TestClient

app = FastAPI()


@app.get("/ok-200")
def ok_200() -> dict:
    return {"ok": True}


@app.post("/created-201", status_code=201)
def created_201() -> dict:
    return {"id": 1}


@app.post("/accepted-202", status_code=202)
def accepted_202() -> dict:
    return {"queued": True}


@app.delete("/no-content-204", status_code=204)
def no_content_204():  # no annotation — 204 forbids body-typed returns
    return None


@app.get("/teapot-418", status_code=418)
def teapot_418() -> dict:
    return {"err": "I'm a teapot"}


client = TestClient(app)


@pytest.mark.parametrize(
    "path,method,expected_status,expected_has_body",
    [
        ("/ok-200", "GET", 200, True),
        ("/created-201", "POST", 201, True),
        ("/accepted-202", "POST", 202, True),
        ("/no-content-204", "DELETE", 204, False),
        ("/teapot-418", "GET", 418, True),
    ],
)
def test_explicit_status_codes(
    path: str, method: str, expected_status: int, expected_has_body: bool
) -> None:
    response = client.request(method, path)
    assert response.status_code == expected_status
    if expected_has_body:
        assert response.content
        assert response.headers["content-type"] == "application/json"
    else:
        assert response.content == b""


def test_status_constants_match_codes() -> None:
    """`fastapi.status` constants align with their numeric values."""
    assert status.HTTP_200_OK == 200
    assert status.HTTP_201_CREATED == 201
    assert status.HTTP_204_NO_CONTENT == 204
    assert status.HTTP_404_NOT_FOUND == 404


def test_404_default_format() -> None:
    response = client.get("/nope")
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


def test_405_default_format() -> None:
    response = client.post("/ok-200")
    assert response.status_code == 405
    assert response.json() == {"detail": "Method Not Allowed"}


def test_response_class_overrides_default_status() -> None:
    """Explicit `Response(status_code=...)` overrides route's `status_code`."""
    explicit_app = FastAPI()

    @explicit_app.get("/x", status_code=200)
    def x() -> Response:
        return Response(content=b"override", status_code=503)

    response = TestClient(explicit_app).get("/x")
    assert response.status_code == 503


def test_304_has_no_body() -> None:
    """304 Not Modified must have no body, like 204."""
    not_modified_app = FastAPI()

    @not_modified_app.get("/cached", status_code=304)
    def cached():
        return None

    response = TestClient(not_modified_app).get("/cached")
    assert response.status_code == 304
    assert response.content == b""
