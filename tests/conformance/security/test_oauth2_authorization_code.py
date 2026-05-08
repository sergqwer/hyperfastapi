"""OAuth2AuthorizationCodeBearer — OpenAPI flows.authorizationCode block."""

from __future__ import annotations

from fastapi import FastAPI, Security
from fastapi.security import OAuth2AuthorizationCodeBearer
from fastapi.testclient import TestClient

app = FastAPI()
oauth2 = OAuth2AuthorizationCodeBearer(
    authorizationUrl="https://example.com/oauth/authorize",
    tokenUrl="https://example.com/oauth/token",
    scopes={"read:items": "Read items", "write:items": "Write items"},
)


@app.get("/me")
def me(token: str = Security(oauth2)) -> dict:
    return {"token": token}


client = TestClient(app)


def test_authorization_code_success() -> None:
    response = client.get("/me", headers={"Authorization": "Bearer mytoken"})
    assert response.status_code == 200
    assert response.json() == {"token": "mytoken"}


def test_authorization_code_missing_returns_401() -> None:
    response = client.get("/me")
    assert response.status_code == 401


def test_authorization_code_in_openapi() -> None:
    schema = client.get("/openapi.json").json()
    schemes = schema["components"]["securitySchemes"]
    assert "OAuth2AuthorizationCodeBearer" in schemes
    s = schemes["OAuth2AuthorizationCodeBearer"]
    assert s["type"] == "oauth2"
    assert "authorizationCode" in s["flows"]


def test_authorization_code_urls_in_flow() -> None:
    schema = client.get("/openapi.json").json()
    flow = schema["components"]["securitySchemes"]["OAuth2AuthorizationCodeBearer"]["flows"][
        "authorizationCode"
    ]
    assert flow["authorizationUrl"] == "https://example.com/oauth/authorize"
    assert flow["tokenUrl"] == "https://example.com/oauth/token"


def test_authorization_code_scopes_in_flow() -> None:
    schema = client.get("/openapi.json").json()
    flow = schema["components"]["securitySchemes"]["OAuth2AuthorizationCodeBearer"]["flows"][
        "authorizationCode"
    ]
    assert flow["scopes"] == {
        "read:items": "Read items",
        "write:items": "Write items",
    }
