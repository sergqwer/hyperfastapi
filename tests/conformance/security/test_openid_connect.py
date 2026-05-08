"""OpenIdConnect — OIDC discovery URL based auth."""

from __future__ import annotations

from fastapi import FastAPI, Security
from fastapi.security import OpenIdConnect
from fastapi.testclient import TestClient

app = FastAPI()
oidc = OpenIdConnect(openIdConnectUrl="https://example.com/.well-known/openid-configuration")


@app.get("/me")
def me(token: str = Security(oidc)) -> dict:
    return {"token": token}


lax_app = FastAPI()
oidc_lax = OpenIdConnect(
    openIdConnectUrl="https://example.com/.well-known/openid-configuration",
    auto_error=False,
)


@lax_app.get("/maybe")
def maybe(token: str | None = Security(oidc_lax)) -> dict:
    return {"token": token}


client = TestClient(app)
lax_client = TestClient(lax_app)


def test_oidc_passthrough_token() -> None:
    """OIDC scheme just delivers the Authorization header value to the handler."""
    response = client.get("/me", headers={"Authorization": "Bearer xyz"})
    assert response.status_code == 200
    assert response.json() == {"token": "Bearer xyz"}


def test_oidc_missing_returns_403() -> None:
    response = client.get("/me")
    assert response.status_code == 403


def test_oidc_auto_error_false_returns_none() -> None:
    response = lax_client.get("/maybe")
    assert response.status_code == 200
    assert response.json() == {"token": None}


def test_oidc_in_openapi() -> None:
    schema = client.get("/openapi.json").json()
    schemes = schema["components"]["securitySchemes"]
    assert "OpenIdConnect" in schemes
    s = schemes["OpenIdConnect"]
    assert s["type"] == "openIdConnect"
    assert s["openIdConnectUrl"] == "https://example.com/.well-known/openid-configuration"


def test_oidc_route_references_scheme() -> None:
    schema = client.get("/openapi.json").json()
    op = schema["paths"]["/me"]["get"]
    sec = op.get("security", [])
    assert any("OpenIdConnect" in entry for entry in sec)
