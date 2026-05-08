"""Advanced Body() — multiple Body params, default_factory, Field constraints, alias."""

from __future__ import annotations

from typing import Annotated

from fastapi import Body, FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field

app = FastAPI()


class Item(BaseModel):
    name: str = Field(..., min_length=1, max_length=50)
    price: float = Field(..., gt=0)
    qty: int = Field(default=1, ge=0)
    tags: list[str] = Field(default_factory=list)


class User(BaseModel):
    name: str


@app.post("/single-with-field-constraints")
def single_with_field_constraints(item: Item) -> dict:
    return item.model_dump()


@app.post("/two-bodies")
def two_bodies(item: Item, user: User) -> dict:
    return {"item": item.model_dump(), "user": user.model_dump()}


@app.post("/two-bodies-mixed")
def two_bodies_mixed(
    item: Item,
    importance: Annotated[int, Body()],
) -> dict:
    return {"item": item.model_dump(), "importance": importance}


@app.post("/embedded-with-meta")
def embedded_with_meta(
    item: Annotated[
        Item, Body(embed=True, description="The item", example={"name": "X", "price": 1.0})
    ],
) -> dict:
    return item.model_dump()


client = TestClient(app)


def test_body_field_constraint_min_length() -> None:
    response = client.post("/single-with-field-constraints", json={"name": "", "price": 1.0})
    assert response.status_code == 422
    err = response.json()["detail"][0]
    assert err["loc"] == ["body", "name"]


def test_body_field_constraint_gt_zero() -> None:
    response = client.post("/single-with-field-constraints", json={"name": "X", "price": 0})
    assert response.status_code == 422
    err = response.json()["detail"][0]
    assert err["loc"] == ["body", "price"]


def test_body_field_default_factory_creates_empty_list() -> None:
    response = client.post(
        "/single-with-field-constraints", json={"name": "X", "price": 1.0}
    )
    assert response.status_code == 200
    assert response.json()["tags"] == []


def test_two_pydantic_bodies_auto_embed() -> None:
    """Two Pydantic body params → JSON envelope {item: ..., user: ...}."""
    response = client.post(
        "/two-bodies",
        json={"item": {"name": "X", "price": 1.0}, "user": {"name": "Bob"}},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["item"]["name"] == "X"
    assert body["user"]["name"] == "Bob"


def test_two_bodies_mixed_pydantic_and_primitive() -> None:
    """A Pydantic body + a primitive Body — both wrapped under their param names."""
    response = client.post(
        "/two-bodies-mixed",
        json={"item": {"name": "X", "price": 1.0}, "importance": 5},
    )
    assert response.status_code == 200
    assert response.json()["importance"] == 5


def test_embedded_with_meta_requires_wrapper_key() -> None:
    response = client.post(
        "/embedded-with-meta",
        json={"item": {"name": "X", "price": 1.0}},
    )
    assert response.status_code == 200
    assert response.json()["name"] == "X"


def test_embedded_with_meta_appears_in_openapi() -> None:
    schema = client.get("/openapi.json").json()
    op = schema["paths"]["/embedded-with-meta"]["post"]
    # Has a request body with the model wrapped under the param name
    assert "requestBody" in op


def test_field_constraint_max_length() -> None:
    response = client.post(
        "/single-with-field-constraints", json={"name": "X" * 51, "price": 1.0}
    )
    assert response.status_code == 422


def test_field_constraint_ge_zero_for_qty() -> None:
    response = client.post(
        "/single-with-field-constraints", json={"name": "X", "price": 1.0, "qty": -1}
    )
    assert response.status_code == 422
    err = response.json()["detail"][0]
    assert err["loc"] == ["body", "qty"]
