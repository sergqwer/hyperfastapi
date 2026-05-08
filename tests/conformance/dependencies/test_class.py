"""Class-as-dependency — callable instances and class-with-Depends-init."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient


# Pattern 1: a callable instance (defines __call__)
class Multiplier:
    def __init__(self, factor: int) -> None:
        self.factor = factor

    def __call__(self, n: int) -> int:
        return n * self.factor


double = Multiplier(2)
triple = Multiplier(3)


# Pattern 2: a class itself as a dependency — FastAPI instantiates it per request
class CommonQueryParams:
    def __init__(self, q: str | None = None, limit: int = 10) -> None:
        self.q = q
        self.limit = limit


app = FastAPI()


@app.get("/double")
def use_double(result: Annotated[int, Depends(double)]) -> dict:
    return {"result": result}


@app.get("/triple")
def use_triple(result: Annotated[int, Depends(triple)]) -> dict:
    return {"result": result}


@app.get("/common")
def use_common(commons: Annotated[CommonQueryParams, Depends(CommonQueryParams)]) -> dict:
    return {"q": commons.q, "limit": commons.limit}


@app.get("/common-shorthand")
def use_common_short(commons: Annotated[CommonQueryParams, Depends()]) -> dict:
    """`Depends()` with no args uses the parameter's annotation as the dependency."""
    return {"q": commons.q, "limit": commons.limit}


client = TestClient(app)


def test_callable_instance_used_as_dependency() -> None:
    response = client.get("/double?n=21")
    assert response.status_code == 200
    assert response.json() == {"result": 42}


def test_separate_callable_instances_keep_their_state() -> None:
    """Each Multiplier instance retains its own factor — they don't bleed."""
    double_resp = client.get("/double?n=5").json()
    triple_resp = client.get("/triple?n=5").json()
    assert double_resp == {"result": 10}
    assert triple_resp == {"result": 15}


def test_class_dependency_instantiated_per_request() -> None:
    response = client.get("/common?q=foo&limit=20")
    assert response.status_code == 200
    assert response.json() == {"q": "foo", "limit": 20}


def test_class_dependency_uses_defaults() -> None:
    response = client.get("/common")
    assert response.status_code == 200
    assert response.json() == {"q": None, "limit": 10}


def test_depends_shorthand_uses_annotation_class() -> None:
    """`Depends()` (empty) uses the param's class annotation as the dependency."""
    response = client.get("/common-shorthand?q=bar&limit=5")
    assert response.status_code == 200
    assert response.json() == {"q": "bar", "limit": 5}


def test_class_dependency_validation_errors_propagate() -> None:
    """A class-dep's __init__ validates query params — bad types yield 422."""
    response = client.get("/common?limit=not-a-number")
    assert response.status_code == 422
