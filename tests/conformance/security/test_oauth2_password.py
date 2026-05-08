"""OAuth2PasswordBearer + OAuth2PasswordRequestForm."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, FastAPI, Security
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from fastapi.testclient import TestClient

app = FastAPI()
oauth2 = OAuth2PasswordBearer(tokenUrl="/token")


@app.get("/me")
def me(token: str = Security(oauth2)) -> dict:
    return {"token": token}


@app.post("/token")
def token_endpoint(form: Annotated[OAuth2PasswordRequestForm, Depends()]) -> dict:
    return {
        "access_token": f"token-for-{form.username}",
        "token_type": "bearer",
        "scopes": form.scopes,
    }


client = TestClient(app)


def test_oauth2_password_bearer_success() -> None:
    response = client.get("/me", headers={"Authorization": "Bearer mytoken"})
    assert response.status_code == 200
    assert response.json() == {"token": "mytoken"}


def test_oauth2_password_bearer_missing_returns_401() -> None:
    response = client.get("/me")
    assert response.status_code == 401
    assert "bearer" in response.headers.get("www-authenticate", "").lower()


def test_oauth2_password_bearer_wrong_scheme_returns_401() -> None:
    response = client.get("/me", headers={"Authorization": "Basic xyz"})
    assert response.status_code == 401


def test_oauth2_password_request_form_success() -> None:
    response = client.post(
        "/token",
        data={"username": "alice", "password": "secret", "grant_type": "password"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["access_token"] == "token-for-alice"
    assert body["token_type"] == "bearer"


def test_oauth2_password_request_form_missing_username_returns_422() -> None:
    response = client.post("/token", data={"password": "secret"})
    assert response.status_code == 422


def test_oauth2_password_request_form_with_scopes() -> None:
    response = client.post(
        "/token",
        data={
            "username": "alice",
            "password": "secret",
            "scope": "read:items write:items",
        },
    )
    assert response.status_code == 200
    assert response.json()["scopes"] == ["read:items", "write:items"]


def test_oauth2_password_bearer_in_openapi() -> None:
    schema = client.get("/openapi.json").json()
    schemes = schema["components"]["securitySchemes"]
    assert "OAuth2PasswordBearer" in schemes
    assert schemes["OAuth2PasswordBearer"]["type"] == "oauth2"
    assert "password" in schemes["OAuth2PasswordBearer"]["flows"]
    assert schemes["OAuth2PasswordBearer"]["flows"]["password"]["tokenUrl"] == "/token"
