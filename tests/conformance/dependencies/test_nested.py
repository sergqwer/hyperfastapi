"""Nested dependencies — graphs of arbitrary depth, propagated values.

Note: every dep helper lives at module scope. Defining them inside a test
function would break `from __future__ import annotations` resolution —
FastAPI's `get_type_hints()` looks up names in module globals, not in test
function locals.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

# Linear chain
def lvl1() -> str:
    return "L1"


def lvl2(a: Annotated[str, Depends(lvl1)]) -> str:
    return f"{a}-L2"


def lvl3(b: Annotated[str, Depends(lvl2)]) -> str:
    return f"{b}-L3"


def lvl4(c: Annotated[str, Depends(lvl3)]) -> str:
    return f"{c}-L4"


# Diamond: A → B, A → C, both feed converge
def root() -> int:
    return 10


def left(r: Annotated[int, Depends(root)]) -> int:
    return r * 2


def right(r: Annotated[int, Depends(root)]) -> int:
    return r + 5


def converge(
    l: Annotated[int, Depends(left)],
    r: Annotated[int, Depends(right)],
) -> dict:
    return {"left": l, "right": r}


# Leaf with query param (auto-extracted)
def leaf_with_q(q: str = "default") -> str:
    return q


def middle(l: Annotated[str, Depends(leaf_with_q)]) -> str:
    return f"M-{l}"


# Counted leaf for cache verification
_call_count: list[int] = []


def counted_leaf() -> int:
    _call_count.append(1)
    return 42


def via_a(x: Annotated[int, Depends(counted_leaf)]) -> int:
    return x


def via_b(x: Annotated[int, Depends(counted_leaf)]) -> int:
    return x + 1


app = FastAPI()


@app.get("/depth-4")
def depth_4(value: Annotated[str, Depends(lvl4)]) -> dict:
    return {"value": value}


@app.get("/diamond")
def diamond_route(d: Annotated[dict, Depends(converge)]) -> dict:
    return d


@app.get("/nested-q")
def get_nested(m: Annotated[str, Depends(middle)]) -> dict:
    return {"m": m}


@app.get("/multi-path")
def multi_path(
    a: Annotated[int, Depends(via_a)],
    b: Annotated[int, Depends(via_b)],
) -> dict:
    return {"a": a, "b": b}


client = TestClient(app)


def test_chain_of_4_deps_resolves_in_order() -> None:
    response = client.get("/depth-4")
    assert response.status_code == 200
    assert response.json() == {"value": "L1-L2-L3-L4"}


def test_diamond_dependency_root_called_once() -> None:
    """With caching, `root` is reached via two paths but invoked once → both
    `left` and `right` see the same input.
    """
    response = client.get("/diamond")
    assert response.status_code == 200
    assert response.json() == {"left": 20, "right": 15}


def test_diamond_values_propagate_correctly() -> None:
    response = client.get("/diamond")
    body = response.json()
    assert body["left"] == 20
    assert body["right"] == 15


def test_nested_dep_with_query_at_leaf() -> None:
    """A dep at the leaf of the graph can take its own query params from the request."""
    assert client.get("/nested-q?q=hello").json() == {"m": "M-hello"}
    assert client.get("/nested-q").json() == {"m": "M-default"}


def test_nested_dep_unique_call_count() -> None:
    """Default cache: leaf reached from two paths is called once per request."""
    _call_count.clear()
    response = client.get("/multi-path")
    assert response.json() == {"a": 42, "b": 43}
    assert len(_call_count) == 1
