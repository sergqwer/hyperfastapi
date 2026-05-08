"""Dependencies — Depends with functions, async, sub-deps, exception propagation."""

from __future__ import annotations

from typing import Annotated

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

app = FastAPI()


def get_query_param(q: str | None = None) -> dict:
    return {"q": q}


async def async_dep() -> str:
    return "from-async"


def parent_dep(child: Annotated[str, Depends(async_dep)]) -> dict:
    return {"chain": child}


def raises_403() -> None:
    raise HTTPException(status_code=403, detail="not allowed")


def returns_none() -> None:
    return None


@app.get("/with-dep")
def with_dep(commons: Annotated[dict, Depends(get_query_param)]) -> dict:
    return commons


@app.get("/with-async-dep")
def with_async_dep(value: Annotated[str, Depends(async_dep)]) -> dict:
    return {"value": value}


@app.get("/with-chain")
def with_chain(parent: Annotated[dict, Depends(parent_dep)]) -> dict:
    return parent


@app.get("/protected")
def protected(_: Annotated[None, Depends(raises_403)]) -> dict:
    return {"ok": True}


@app.get("/none-dep")
def none_dep(value: Annotated[None, Depends(returns_none)]) -> dict:
    return {"value": value}


client = TestClient(app)


def test_function_dependency_resolves() -> None:
    response = client.get("/with-dep?q=hello")
    assert response.status_code == 200
    assert response.json() == {"q": "hello"}


def test_function_dependency_with_default() -> None:
    response = client.get("/with-dep")
    assert response.status_code == 200
    assert response.json() == {"q": None}


def test_async_dependency_resolves() -> None:
    response = client.get("/with-async-dep")
    assert response.status_code == 200
    assert response.json() == {"value": "from-async"}


def test_chained_dependencies_resolve() -> None:
    """A dep that depends on another dep — both must be called and values bubbled up."""
    response = client.get("/with-chain")
    assert response.status_code == 200
    assert response.json() == {"chain": "from-async"}


def test_dependency_raising_httpexception_returns_that_status() -> None:
    response = client.get("/protected")
    assert response.status_code == 403
    assert response.json() == {"detail": "not allowed"}


def test_dependency_returning_none_passes_none_to_handler() -> None:
    response = client.get("/none-dep")
    assert response.status_code == 200
    assert response.json() == {"value": None}


# Counter to verify per-request caching in next test
_call_count = 0


def counted_dep() -> int:
    global _call_count
    _call_count += 1
    return _call_count


def using_counted_a(x: Annotated[int, Depends(counted_dep)]) -> int:
    return x


def using_counted_b(y: Annotated[int, Depends(counted_dep)]) -> int:
    return y


cache_app = FastAPI()


@cache_app.get("/cached")
def cached_route(
    a: Annotated[int, Depends(using_counted_a)],
    b: Annotated[int, Depends(using_counted_b)],
) -> dict:
    return {"a": a, "b": b}


cache_client = TestClient(cache_app)


def test_dependencies_are_cached_per_request_by_default() -> None:
    """`use_cache=True` (default): the SAME dependency reached via two paths in one request
    must be invoked exactly once. So `a` and `b` see the same counter value.
    """
    global _call_count
    _call_count = 0
    response = cache_client.get("/cached")
    assert response.status_code == 200
    body = response.json()
    # Both should reflect the same single invocation
    assert body["a"] == body["b"] == 1
    assert _call_count == 1
