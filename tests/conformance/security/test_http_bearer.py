"""HTTPBearer security scheme — Authorization: Bearer <token>."""

from __future__ import annotations

import pytest
from fastapi import FastAPI, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.testclient import TestClient

# auto_error=True (default): missing/wrong creds → 403/401 automatically
app_strict = FastAPI()
bearer_strict = HTTPBearer()


@app_strict.get("/me")
def read_me_strict(
    creds: HTTPAuthorizationCredentials = Security(bearer_strict),
) -> dict:
    return {"scheme": creds.scheme, "credentials": creds.credentials}


# auto_error=False: missing → None, handler decides
app_lax = FastAPI()
bearer_lax = HTTPBearer(auto_error=False)


@app_lax.get("/maybe-me")
def maybe_me(
    creds: HTTPAuthorizationCredentials | None = Security(bearer_lax),
) -> dict:
    if creds is None:
        return {"authenticated": False}
    return {"authenticated": True, "credentials": creds.credentials}


client_strict = TestClient(app_strict)
client_lax = TestClient(app_lax)


def test_http_bearer_success() -> None:
    response = client_strict.get("/me", headers={"Authorization": "Bearer foobar"})
    assert response.status_code == 200, response.text
    assert response.json() == {"scheme": "Bearer", "credentials": "foobar"}


def test_http_bearer_no_credentials_returns_403() -> None:
    """auto_error=True: missing Authorization header → 403 with 'Not authenticated'."""
    response = client_strict.get("/me")
    assert response.status_code == 403
    assert response.json() == {"detail": "Not authenticated"}


def test_http_bearer_wrong_scheme_returns_403() -> None:
    """auto_error=True: 'Authorization: Basic ...' is rejected for a Bearer scheme."""
    response = client_strict.get(
        "/me",
        headers={"Authorization": "Basic dXNlcjpwYXNz"},
    )
    assert response.status_code == 403


def test_http_bearer_auto_error_false_lets_through_no_creds() -> None:
    """auto_error=False: missing creds → handler receives None instead of erroring."""
    response = client_lax.get("/maybe-me")
    assert response.status_code == 200
    assert response.json() == {"authenticated": False}


def test_http_bearer_auto_error_false_with_valid_creds() -> None:
    response = client_lax.get(
        "/maybe-me", headers={"Authorization": "Bearer secrettoken"}
    )
    assert response.status_code == 200
    assert response.json() == {"authenticated": True, "credentials": "secrettoken"}


def test_http_bearer_appears_in_openapi() -> None:
    schema = client_strict.get("/openapi.json").json()
    assert "HTTPBearer" in schema["components"]["securitySchemes"]
    bearer_def = schema["components"]["securitySchemes"]["HTTPBearer"]
    assert bearer_def["type"] == "http"
    assert bearer_def["scheme"] == "bearer"
