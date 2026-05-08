"""Router/route-level `dependencies=[]` — applied to all routes within."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException
from fastapi.testclient import TestClient


def require_token(x_token: Annotated[str | None, Header()] = None) -> None:
    if x_token != "secret":
        raise HTTPException(status_code=401, detail="bad token")


def require_admin(x_role: Annotated[str | None, Header()] = None) -> None:
    if x_role != "admin":
        raise HTTPException(status_code=403, detail="not admin")


# App-level dependencies on FastAPI()
app = FastAPI(dependencies=[Depends(require_token)])


@app.get("/anything")
def anything() -> dict:
    return {"ok": True}


# Router-level dependencies on APIRouter()
admin_router = APIRouter(dependencies=[Depends(require_admin)])


@admin_router.get("/admin/users")
def admin_users() -> dict:
    return {"users": []}


app.include_router(admin_router)

# Route-level dependencies via the decorator
@app.get("/scoped", dependencies=[Depends(require_token)])
def scoped() -> dict:
    return {"scoped": True}


client = TestClient(app)


def test_app_level_dep_blocks_all_routes() -> None:
    response = client.get("/anything")
    assert response.status_code == 401
    assert response.json() == {"detail": "bad token"}


def test_app_level_dep_allows_with_token() -> None:
    response = client.get("/anything", headers={"X-Token": "secret"})
    assert response.status_code == 200


def test_router_level_dep_stacks_with_app_level() -> None:
    """Router-level dep + app-level dep = both must pass."""
    # Token only — admin check fails
    response = client.get(
        "/admin/users", headers={"X-Token": "secret"}
    )
    assert response.status_code == 403

    # Both headers — passes
    response = client.get(
        "/admin/users", headers={"X-Token": "secret", "X-Role": "admin"}
    )
    assert response.status_code == 200
    assert response.json() == {"users": []}


def test_router_level_dep_blocks_when_token_missing() -> None:
    """App-level dep is checked first; missing token fails before admin check."""
    response = client.get("/admin/users", headers={"X-Role": "admin"})
    assert response.status_code == 401


def test_route_level_dep_runs_in_order() -> None:
    """Route-level `dependencies=[]` are checked along with app-level."""
    response = client.get("/scoped")
    assert response.status_code == 401  # app-level token check fails first

    response = client.get("/scoped", headers={"X-Token": "secret"})
    assert response.status_code == 200
    assert response.json() == {"scoped": True}
