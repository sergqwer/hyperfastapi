"""Header parameters: convert_underscores, alias, list, case-insensitive lookup."""

from __future__ import annotations

from typing import Annotated

from fastapi import FastAPI, Header
from fastapi.testclient import TestClient

app = FastAPI()


@app.get("/required")
def required_header(x_token: Annotated[str, Header()]) -> dict:
    return {"x_token": x_token}


@app.get("/optional")
def optional_header(x_token: Annotated[str | None, Header()] = None) -> dict:
    return {"x_token": x_token}


@app.get("/no-convert")
def no_convert(x_token: Annotated[str, Header(convert_underscores=False)]) -> dict:
    return {"x_token": x_token}


@app.get("/aliased")
def aliased(my: Annotated[str, Header(alias="Authorization")]) -> dict:
    return {"auth": my}


@app.get("/list")
def list_header(x_tag: Annotated[list[str], Header()] = []) -> dict:
    return {"x_tag": x_tag}


client = TestClient(app)


def test_header_underscore_to_dash_default() -> None:
    """`x_token` Python param maps to `X-Token` HTTP header by default."""
    response = client.get("/required", headers={"X-Token": "abc"})
    assert response.status_code == 200
    assert response.json() == {"x_token": "abc"}


def test_header_case_insensitive_lookup() -> None:
    """HTTP headers are case-insensitive; the same param matches all casings."""
    response = client.get("/required", headers={"x-token": "abc"})
    assert response.status_code == 200
    assert response.json() == {"x_token": "abc"}


def test_header_required_missing_returns_422() -> None:
    response = client.get("/required")
    assert response.status_code == 422
    err = response.json()["detail"][0]
    assert err["loc"][0] == "header"


def test_header_optional_missing_is_none() -> None:
    response = client.get("/optional")
    assert response.status_code == 200
    assert response.json() == {"x_token": None}


def test_header_optional_present() -> None:
    response = client.get("/optional", headers={"X-Token": "xyz"})
    assert response.status_code == 200
    assert response.json() == {"x_token": "xyz"}


def test_header_no_convert_underscores() -> None:
    """convert_underscores=False keeps the underscore form (rarely used in HTTP)."""
    # HTTP doesn't normally allow underscores, but TestClient does pass through
    response = client.get("/no-convert", headers={"x_token": "noconvert"})
    assert response.status_code == 200


def test_header_alias() -> None:
    response = client.get("/aliased", headers={"Authorization": "Bearer x"})
    assert response.status_code == 200
    assert response.json() == {"auth": "Bearer x"}


def test_header_list_multiple_values() -> None:
    """Multiple headers with the same name come back as a list."""
    response = client.get(
        "/list", headers=[("X-Tag", "a"), ("X-Tag", "b"), ("X-Tag", "c")]
    )
    assert response.status_code == 200
    assert response.json() == {"x_tag": ["a", "b", "c"]}


def test_header_list_empty_default() -> None:
    response = client.get("/list")
    assert response.status_code == 200
    assert response.json() == {"x_tag": []}


def test_header_validation_error_loc_format() -> None:
    """Error loc for missing header: ['header', '<name>']."""
    response = client.get("/required")
    assert response.status_code == 422
    err = response.json()["detail"][0]
    assert err["loc"] == ["header", "x-token"]
