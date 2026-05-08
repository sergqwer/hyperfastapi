"""HTTPBasic — Basic auth (Authorization: Basic <base64>)."""

from __future__ import annotations

import base64

from fastapi import FastAPI, Security
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.testclient import TestClient

app = FastAPI()
basic = HTTPBasic()


@app.get("/me")
def me(creds: HTTPBasicCredentials = Security(basic)) -> dict:
    return {"username": creds.username}


lax_app = FastAPI()
basic_lax = HTTPBasic(auto_error=False)


@lax_app.get("/maybe")
def maybe(creds: HTTPBasicCredentials | None = Security(basic_lax)) -> dict:
    return {"authenticated": creds is not None}


client = TestClient(app)
lax_client = TestClient(lax_app)


def _basic_header(user: str, password: str) -> str:
    encoded = base64.b64encode(f"{user}:{password}".encode()).decode()
    return f"Basic {encoded}"


def test_http_basic_success() -> None:
    response = client.get("/me", headers={"Authorization": _basic_header("alice", "pwd")})
    assert response.status_code == 200
    assert response.json() == {"username": "alice"}


def test_http_basic_no_credentials_returns_401() -> None:
    response = client.get("/me")
    assert response.status_code == 401
    # WWW-Authenticate header should be set so browsers prompt
    assert "basic" in response.headers.get("www-authenticate", "").lower()


def test_http_basic_wrong_scheme_returns_401() -> None:
    response = client.get("/me", headers={"Authorization": "Bearer xyz"})
    assert response.status_code == 401


def test_http_basic_malformed_returns_401() -> None:
    response = client.get("/me", headers={"Authorization": "Basic not-base64!"})
    assert response.status_code == 401


def test_http_basic_auto_error_false_lets_through() -> None:
    response = lax_client.get("/maybe")
    assert response.status_code == 200
    assert response.json() == {"authenticated": False}


def test_http_basic_auto_error_false_with_creds() -> None:
    response = lax_client.get(
        "/maybe", headers={"Authorization": _basic_header("u", "p")}
    )
    assert response.status_code == 200
    assert response.json() == {"authenticated": True}


def test_http_basic_in_openapi() -> None:
    schema = client.get("/openapi.json").json()
    schemes = schema["components"]["securitySchemes"]
    assert "HTTPBasic" in schemes
    assert schemes["HTTPBasic"]["type"] == "http"
    assert schemes["HTTPBasic"]["scheme"] == "basic"
