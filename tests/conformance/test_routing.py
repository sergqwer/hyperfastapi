"""Routing — HTTP-method decorators, APIRouter, include_router, response_model.

Each test mounts its own FastAPI app at module scope where reasonable.
"""

from __future__ import annotations

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel


# ---------------------------------------------------------------------------
# All HTTP methods
# ---------------------------------------------------------------------------

method_app = FastAPI()


@method_app.get("/m")
def _m_get() -> dict:
    return {"m": "GET"}


@method_app.post("/m")
def _m_post() -> dict:
    return {"m": "POST"}


@method_app.put("/m")
def _m_put() -> dict:
    return {"m": "PUT"}


@method_app.delete("/m")
def _m_delete() -> dict:
    return {"m": "DELETE"}


@method_app.patch("/m")
def _m_patch() -> dict:
    return {"m": "PATCH"}


@method_app.options("/m")
def _m_options() -> dict:
    return {"m": "OPTIONS"}


@method_app.head("/m")
def _m_head() -> None:
    return None


method_client = TestClient(method_app)


@pytest.mark.parametrize(
    "method,expected_body",
    [
        ("GET", {"m": "GET"}),
        ("POST", {"m": "POST"}),
        ("PUT", {"m": "PUT"}),
        ("DELETE", {"m": "DELETE"}),
        ("PATCH", {"m": "PATCH"}),
        ("OPTIONS", {"m": "OPTIONS"}),
    ],
)
def test_http_method_decorator_registers_route(method: str, expected_body: dict) -> None:
    response = method_client.request(method, "/m")
    assert response.status_code == 200, response.text
    assert response.json() == expected_body


def test_head_returns_no_body() -> None:
    """HEAD method must not include a body, regardless of handler return."""
    response = method_client.request("HEAD", "/m")
    assert response.status_code == 200
    assert response.content == b""


# ---------------------------------------------------------------------------
# api_route, status_code, response_model
# ---------------------------------------------------------------------------


def test_api_route_with_multiple_methods() -> None:
    app = FastAPI()

    @app.api_route("/multi", methods=["GET", "POST"])
    def multi() -> dict:
        return {"ok": True}

    client = TestClient(app)
    assert client.get("/multi").status_code == 200
    assert client.post("/multi").status_code == 200
    # PUT was not declared — must be 405
    assert client.put("/multi").status_code == 405


def test_explicit_status_code_on_route() -> None:
    app = FastAPI()

    @app.post("/created", status_code=201)
    def make() -> dict:
        return {"id": 1}

    response = TestClient(app).post("/created")
    assert response.status_code == 201
    assert response.json() == {"id": 1}


def test_response_model_filters_extra_fields() -> None:
    """Fields returned by handler that aren't in `response_model` must be dropped from JSON."""

    class PublicItem(BaseModel):
        name: str

    app = FastAPI()

    @app.get("/item", response_model=PublicItem)
    def get_item() -> dict:
        return {"name": "Foo", "internal_secret": "shhh"}

    response = TestClient(app).get("/item")
    assert response.status_code == 200
    body = response.json()
    assert body == {"name": "Foo"}
    assert "internal_secret" not in body


# ---------------------------------------------------------------------------
# APIRouter + include_router
# ---------------------------------------------------------------------------


def test_apirouter_mounted_with_prefix() -> None:
    """include_router(prefix=...) prepends to all routes from the router."""
    app = FastAPI()
    router = APIRouter()

    @router.get("/items")
    def list_items() -> list:
        return []

    app.include_router(router, prefix="/api/v1")

    client = TestClient(app)
    assert client.get("/api/v1/items").status_code == 200
    # Without the prefix it must NOT match
    assert client.get("/items").status_code == 404


def test_apirouter_prefix_concatenation_through_include() -> None:
    """A router with its own prefix, included with another prefix, concatenates them."""
    app = FastAPI()
    inner = APIRouter(prefix="/users")

    @inner.get("/me")
    def me() -> dict:
        return {"who": "me"}

    app.include_router(inner, prefix="/api")

    response = TestClient(app).get("/api/users/me")
    assert response.status_code == 200
    assert response.json() == {"who": "me"}


def test_router_tags_propagate_to_routes() -> None:
    """Tags set on the router (or on include_router) attach to every route in OpenAPI."""
    app = FastAPI()
    router = APIRouter(tags=["users"])

    @router.get("/profile")
    def profile() -> dict:
        return {}

    app.include_router(router)

    schema = TestClient(app).get("/openapi.json").json()
    op = schema["paths"]["/profile"]["get"]
    assert "users" in op.get("tags", []), f"Expected 'users' tag on /profile GET, got {op.get('tags')}"


def test_route_with_unsupported_method_returns_405() -> None:
    """A path that exists for one method must reject other methods with 405."""
    app = FastAPI()

    @app.get("/only-get")
    def only_get() -> dict:
        return {}

    response = TestClient(app).post("/only-get")
    assert response.status_code == 405


def test_nonexistent_path_returns_404() -> None:
    app = FastAPI()
    response = TestClient(app).get("/nope")
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}
