"""APIKeyHeader — API key in a custom HTTP header."""

from __future__ import annotations

from fastapi import FastAPI, Security
from fastapi.security import APIKeyHeader
from fastapi.testclient import TestClient

app = FastAPI()
api_key = APIKeyHeader(name="X-API-Key")


@app.get("/protected")
def protected(key: str = Security(api_key)) -> dict:
    return {"key": key}


lax_app = FastAPI()
api_key_lax = APIKeyHeader(name="X-API-Key", auto_error=False)


@lax_app.get("/maybe")
def maybe(key: str | None = Security(api_key_lax)) -> dict:
    return {"key": key}


client = TestClient(app)
lax_client = TestClient(lax_app)


def test_apikey_header_success() -> None:
    response = client.get("/protected", headers={"X-API-Key": "abc123"})
    assert response.status_code == 200
    assert response.json() == {"key": "abc123"}


def test_apikey_header_missing_returns_403() -> None:
    response = client.get("/protected")
    assert response.status_code == 403


def test_apikey_header_wrong_header_name_returns_403() -> None:
    response = client.get("/protected", headers={"X-Wrong-Name": "abc"})
    assert response.status_code == 403


def test_apikey_header_auto_error_false_returns_none() -> None:
    response = lax_client.get("/maybe")
    assert response.status_code == 200
    assert response.json() == {"key": None}


def test_apikey_header_in_openapi() -> None:
    schema = client.get("/openapi.json").json()
    schemes = schema["components"]["securitySchemes"]
    assert "APIKeyHeader" in schemes
    assert schemes["APIKeyHeader"]["type"] == "apiKey"
    assert schemes["APIKeyHeader"]["in"] == "header"
    assert schemes["APIKeyHeader"]["name"] == "X-API-Key"
