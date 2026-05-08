"""APIKeyCookie — API key in a cookie."""

from __future__ import annotations

from fastapi import FastAPI, Security
from fastapi.security import APIKeyCookie
from fastapi.testclient import TestClient

app = FastAPI()
api_key = APIKeyCookie(name="session")


@app.get("/protected")
def protected(key: str = Security(api_key)) -> dict:
    return {"key": key}


client = TestClient(app)


def test_apikey_cookie_success() -> None:
    response = client.get("/protected", cookies={"session": "abc123"})
    assert response.status_code == 200
    assert response.json() == {"key": "abc123"}


def test_apikey_cookie_missing_returns_403() -> None:
    response = client.get("/protected")
    assert response.status_code == 403


def test_apikey_cookie_wrong_name_returns_403() -> None:
    response = client.get("/protected", cookies={"wrong": "abc"})
    assert response.status_code == 403


def test_apikey_cookie_in_openapi() -> None:
    schema = client.get("/openapi.json").json()
    schemes = schema["components"]["securitySchemes"]
    assert "APIKeyCookie" in schemes
    assert schemes["APIKeyCookie"]["in"] == "cookie"
    assert schemes["APIKeyCookie"]["name"] == "session"
