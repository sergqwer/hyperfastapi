"""Dependency caching — `use_cache=True/False` and per-request behavior."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

calls: list[str] = []


def expensive() -> int:
    calls.append("expensive")
    return 42


def via_a(x: Annotated[int, Depends(expensive)]) -> int:
    return x + 1


def via_b(x: Annotated[int, Depends(expensive)]) -> int:
    return x + 2


def via_a_no_cache(
    x: Annotated[int, Depends(expensive, use_cache=False)],
) -> int:
    return x + 1


def via_b_no_cache(
    x: Annotated[int, Depends(expensive, use_cache=False)],
) -> int:
    return x + 2


app = FastAPI()


@app.get("/cached")
def cached_route(
    a: Annotated[int, Depends(via_a)],
    b: Annotated[int, Depends(via_b)],
) -> dict:
    return {"a": a, "b": b}


@app.get("/uncached")
def uncached_route(
    a: Annotated[int, Depends(via_a_no_cache)],
    b: Annotated[int, Depends(via_b_no_cache)],
) -> dict:
    return {"a": a, "b": b}


client = TestClient(app)


def test_default_caching_calls_dep_once_per_request() -> None:
    calls.clear()
    response = client.get("/cached")
    assert response.status_code == 200
    assert response.json() == {"a": 43, "b": 44}
    assert len(calls) == 1


def test_use_cache_false_calls_dep_each_time() -> None:
    calls.clear()
    response = client.get("/uncached")
    assert response.status_code == 200
    assert response.json() == {"a": 43, "b": 44}
    # Two distinct uses, each invokes the dep
    assert len(calls) == 2


def test_cache_does_not_persist_across_requests() -> None:
    """The cache is request-scoped; new request → new invocation."""
    calls.clear()
    client.get("/cached")
    client.get("/cached")
    # Two requests, each calls the dep once → 2 total
    assert len(calls) == 2


def test_uncached_two_requests() -> None:
    calls.clear()
    client.get("/uncached")
    client.get("/uncached")
    # Two requests × 2 calls each = 4
    assert len(calls) == 4


def test_uncached_dep_returns_correct_values() -> None:
    """Even without caching, semantics must be correct."""
    calls.clear()
    response = client.get("/uncached")
    assert response.json() == {"a": 43, "b": 44}
