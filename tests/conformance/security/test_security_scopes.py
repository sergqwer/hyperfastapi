"""SecurityScopes — fine-grained scope checking."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Security
from fastapi.security import OAuth2PasswordBearer, SecurityScopes
from fastapi.testclient import TestClient

app = FastAPI()
oauth2 = OAuth2PasswordBearer(
    tokenUrl="/token",
    scopes={
        "read:items": "Read items",
        "write:items": "Write items",
        "admin": "Admin access",
    },
)


def get_user(
    security_scopes: SecurityScopes,
    token: str = Depends(oauth2),
) -> dict:
    """A toy auth that decodes 'token' as a comma-separated scope list."""
    granted = set(token.split(","))
    for required in security_scopes.scopes:
        if required not in granted:
            raise HTTPException(
                status_code=403,
                detail=f"Missing scope: {required}",
                headers={"WWW-Authenticate": f'Bearer scope="{security_scopes.scope_str}"'},
            )
    return {"granted": list(granted)}


@app.get("/items/", dependencies=[Security(get_user, scopes=["read:items"])])
def list_items() -> dict:
    return {"items": []}


@app.post("/items/", dependencies=[Security(get_user, scopes=["write:items"])])
def create_item() -> dict:
    return {"created": True}


@app.delete("/admin", dependencies=[Security(get_user, scopes=["admin", "write:items"])])
def admin_delete() -> dict:
    return {"deleted": True}


client = TestClient(app)


def test_scope_satisfied_grants_access() -> None:
    response = client.get(
        "/items/", headers={"Authorization": "Bearer read:items"}
    )
    assert response.status_code == 200


def test_scope_not_satisfied_returns_403() -> None:
    response = client.get(
        "/items/", headers={"Authorization": "Bearer write:items"}
    )
    assert response.status_code == 403
    assert "read:items" in response.json()["detail"]


def test_multiple_scopes_all_required() -> None:
    """When two scopes are required, both must be in the token."""
    # Only one of two — fails
    response = client.delete("/admin", headers={"Authorization": "Bearer admin"})
    assert response.status_code == 403

    # Both present — passes
    response = client.delete(
        "/admin", headers={"Authorization": "Bearer admin,write:items"}
    )
    assert response.status_code == 200


def test_scopes_appear_in_openapi_definition() -> None:
    schema = client.get("/openapi.json").json()
    flows = schema["components"]["securitySchemes"]["OAuth2PasswordBearer"]["flows"]
    scopes = flows["password"]["scopes"]
    assert "read:items" in scopes
    assert "write:items" in scopes
    assert "admin" in scopes


def test_route_has_security_with_scopes_in_openapi() -> None:
    schema = client.get("/openapi.json").json()
    op = schema["paths"]["/items/"]["get"]
    sec = op.get("security", [])
    assert sec, "Route should have a security entry"
    # Scopes are listed in the security item
    bearer_entry = next((s for s in sec if "OAuth2PasswordBearer" in s), None)
    assert bearer_entry is not None
    assert "read:items" in bearer_entry["OAuth2PasswordBearer"]
