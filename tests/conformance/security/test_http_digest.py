"""HTTPDigest — Digest auth (Authorization: Digest <params>)."""

from __future__ import annotations

from fastapi import FastAPI, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPDigest
from fastapi.testclient import TestClient

app = FastAPI()
digest = HTTPDigest()


@app.get("/me")
def me(creds: HTTPAuthorizationCredentials = Security(digest)) -> dict:
    return {"scheme": creds.scheme, "credentials": creds.credentials}


lax_app = FastAPI()
digest_lax = HTTPDigest(auto_error=False)


@lax_app.get("/maybe")
def maybe(creds: HTTPAuthorizationCredentials | None = Security(digest_lax)) -> dict:
    return {"creds": None if creds is None else creds.credentials}


client = TestClient(app)
lax_client = TestClient(lax_app)


def test_http_digest_success() -> None:
    response = client.get("/me", headers={"Authorization": 'Digest username="u",nonce="n"'})
    assert response.status_code == 200
    body = response.json()
    assert body["scheme"] == "Digest"
    assert "username" in body["credentials"]


def test_http_digest_missing_returns_403() -> None:
    response = client.get("/me")
    assert response.status_code == 403


def test_http_digest_wrong_scheme_returns_403() -> None:
    """Bearer credentials must NOT be accepted on a Digest endpoint."""
    response = client.get("/me", headers={"Authorization": "Bearer xyz"})
    assert response.status_code == 403


def test_http_digest_auto_error_false_lets_through() -> None:
    response = lax_client.get("/maybe")
    assert response.status_code == 200
    assert response.json() == {"creds": None}


def test_http_digest_in_openapi() -> None:
    schema = client.get("/openapi.json").json()
    schemes = schema["components"]["securitySchemes"]
    assert "HTTPDigest" in schemes
    s = schemes["HTTPDigest"]
    assert s["type"] == "http"
    assert s["scheme"] == "digest"
