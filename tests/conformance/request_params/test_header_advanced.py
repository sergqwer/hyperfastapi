"""Advanced Header() — deprecated, examples, validation."""

from __future__ import annotations

from typing import Annotated

from fastapi import FastAPI, Header
from fastapi.testclient import TestClient

app = FastAPI()


@app.get("/dep-header")
def dep_header(
    x_legacy: Annotated[str | None, Header(deprecated=True)] = None,
) -> dict:
    return {"x_legacy": x_legacy}


@app.get("/typed-int-header")
def typed_int(x_count: Annotated[int, Header()]) -> dict:
    return {"x_count": x_count}


@app.get("/with-pattern")
def with_pattern(x_id: Annotated[str, Header(pattern=r"^[a-f0-9]{8}$")]) -> dict:
    return {"x_id": x_id}


client = TestClient(app)


def test_deprecated_header_marker_in_openapi() -> None:
    schema = client.get("/openapi.json").json()
    op = schema["paths"]["/dep-header"]["get"]
    legacy = next(p for p in op["parameters"] if p["name"] == "x-legacy")
    assert legacy.get("deprecated") is True


def test_typed_int_header_parses() -> None:
    response = client.get("/typed-int-header", headers={"X-Count": "42"})
    assert response.status_code == 200
    assert response.json() == {"x_count": 42}


def test_typed_int_header_rejects_non_numeric() -> None:
    response = client.get("/typed-int-header", headers={"X-Count": "abc"})
    assert response.status_code == 422


def test_header_pattern_validates() -> None:
    response = client.get("/with-pattern", headers={"X-Id": "deadbeef"})
    assert response.status_code == 200
    assert response.json() == {"x_id": "deadbeef"}


def test_header_pattern_rejects_mismatch() -> None:
    response = client.get("/with-pattern", headers={"X-Id": "TOO-LONG-NAME"})
    assert response.status_code == 422
