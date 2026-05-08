"""APIKeyQuery — API key in a query parameter."""

from __future__ import annotations

from fastapi import FastAPI, Security
from fastapi.security import APIKeyQuery
from fastapi.testclient import TestClient

app = FastAPI()
api_key = APIKeyQuery(name="api_key")


@app.get("/protected")
def protected(key: str = Security(api_key)) -> dict:
    return {"key": key}


client = TestClient(app)


def test_apikey_query_success() -> None:
    response = client.get("/protected?api_key=abc123")
    assert response.status_code == 200
    assert response.json() == {"key": "abc123"}


def test_apikey_query_missing_returns_403() -> None:
    response = client.get("/protected")
    assert response.status_code == 403


def test_apikey_query_wrong_param_returns_403() -> None:
    response = client.get("/protected?wrong=abc")
    assert response.status_code == 403


def test_apikey_query_in_openapi() -> None:
    schema = client.get("/openapi.json").json()
    schemes = schema["components"]["securitySchemes"]
    assert "APIKeyQuery" in schemes
    assert schemes["APIKeyQuery"]["in"] == "query"
    assert schemes["APIKeyQuery"]["name"] == "api_key"


def test_apikey_query_empty_value_treated_as_absent() -> None:
    """`?api_key=` (empty string) is treated as absent → 403, not as present-with-empty.

    Same behavior as missing entirely — FastAPI rejects falsy values to avoid
    silent passthrough of misconfigured clients.
    """
    response = client.get("/protected?api_key=")
    assert response.status_code == 403
