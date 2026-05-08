"""APIRouter depth — nested routers, prefix/tag propagation, dependencies, schema visibility."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Nested 3 levels: app → outer → middle → inner
# ---------------------------------------------------------------------------

inner = APIRouter(prefix="/inner", tags=["inner"])


@inner.get("/leaf")
def leaf() -> dict:
    return {"reached": "leaf"}


middle = APIRouter(prefix="/middle", tags=["middle"])
middle.include_router(inner)


outer = APIRouter(prefix="/outer", tags=["outer"])
outer.include_router(middle)


nested_app = FastAPI()
nested_app.include_router(outer)

nested_client = TestClient(nested_app)


def test_three_level_nested_router_path() -> None:
    """Prefixes concatenate left-to-right: /outer + /middle + /inner + /leaf."""
    response = nested_client.get("/outer/middle/inner/leaf")
    assert response.status_code == 200
    assert response.json() == {"reached": "leaf"}


def test_three_level_nested_tags_all_present() -> None:
    """Every tag along the chain attaches to the leaf operation."""
    schema = nested_client.get("/openapi.json").json()
    op = schema["paths"]["/outer/middle/inner/leaf"]["get"]
    assert set(op["tags"]) == {"outer", "middle", "inner"}


# ---------------------------------------------------------------------------
# include_router(prefix=) overrides router prefix
# ---------------------------------------------------------------------------


def test_include_router_extra_prefix_concatenates() -> None:
    inner_router = APIRouter(prefix="/v1")

    @inner_router.get("/health")
    def health() -> dict:
        return {"v": 1}

    app = FastAPI()
    app.include_router(inner_router, prefix="/api")

    response = TestClient(app).get("/api/v1/health")
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Router-level dependencies that block routes
# ---------------------------------------------------------------------------


def require_token(x_token: Annotated[str | None, Header()] = None) -> None:
    if x_token != "ok":
        raise HTTPException(status_code=401, detail="bad")


protected_router = APIRouter(dependencies=[Depends(require_token)])


@protected_router.get("/secret")
def secret() -> dict:
    return {"secret": "value"}


protected_app = FastAPI()
protected_app.include_router(protected_router)
protected_client = TestClient(protected_app)


def test_router_dependency_blocks_when_missing() -> None:
    response = protected_client.get("/secret")
    assert response.status_code == 401


def test_router_dependency_passes_with_header() -> None:
    response = protected_client.get("/secret", headers={"X-Token": "ok"})
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# include_in_schema propagation through include_router
# ---------------------------------------------------------------------------


def test_include_in_schema_false_on_route_hides_from_openapi() -> None:
    app = FastAPI()
    router = APIRouter()

    @router.get("/visible")
    def visible() -> dict:
        return {}

    @router.get("/hidden", include_in_schema=False)
    def hidden() -> dict:
        return {}

    app.include_router(router)
    schema = TestClient(app).get("/openapi.json").json()
    assert "/visible" in schema["paths"]
    assert "/hidden" not in schema["paths"]


def test_include_router_include_in_schema_false_hides_all() -> None:
    """Passing include_in_schema=False to include_router hides the whole router."""
    app = FastAPI()
    router = APIRouter()

    @router.get("/r1")
    def r1() -> dict:
        return {}

    @router.get("/r2")
    def r2() -> dict:
        return {}

    app.include_router(router, include_in_schema=False)
    schema = TestClient(app).get("/openapi.json").json()
    assert "/r1" not in schema["paths"]
    assert "/r2" not in schema["paths"]
    # But routes still respond at runtime
    assert TestClient(app).get("/r1").status_code == 200


# ---------------------------------------------------------------------------
# Same path on two routers — second include_router fails or dedupes?
# ---------------------------------------------------------------------------


def test_two_routers_same_path_first_wins() -> None:
    """When two routers register the same path, FastAPI uses first-match semantics."""
    app = FastAPI()
    r1 = APIRouter()
    r2 = APIRouter()

    @r1.get("/dup")
    def from_r1() -> dict:
        return {"who": "r1"}

    @r2.get("/dup")
    def from_r2() -> dict:
        return {"who": "r2"}

    app.include_router(r1)
    app.include_router(r2)
    response = TestClient(app).get("/dup")
    assert response.status_code == 200
    assert response.json() == {"who": "r1"}


# ---------------------------------------------------------------------------
# Tag inheritance via include_router(tags=...)
# ---------------------------------------------------------------------------


def test_tags_from_include_router_added_to_routes() -> None:
    app = FastAPI()
    router = APIRouter()

    @router.get("/r")
    def r() -> dict:
        return {}

    app.include_router(router, tags=["from-include"])
    schema = TestClient(app).get("/openapi.json").json()
    assert "from-include" in schema["paths"]["/r"]["get"]["tags"]


# ---------------------------------------------------------------------------
# trailing-slash behavior (redirect_slashes default = True)
# ---------------------------------------------------------------------------


def test_trailing_slash_redirect_default() -> None:
    """Default redirect_slashes=True: /path/ → 307 redirect to /path."""
    app = FastAPI()

    @app.get("/items")
    def items() -> dict:
        return {}

    client = TestClient(app)
    response = client.get("/items/", follow_redirects=False)
    # Either redirects to /items, or matches directly
    assert response.status_code in (200, 307)
    if response.status_code == 307:
        # Location may be absolute (http://testserver/items) or relative (/items)
        loc = response.headers["location"].rstrip("/")
        assert loc.endswith("/items"), f"Unexpected redirect location: {loc!r}"
