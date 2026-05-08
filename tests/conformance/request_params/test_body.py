"""Request body: Pydantic model, nested, list, embed, validation errors."""

from __future__ import annotations

from typing import Annotated

import pytest
from fastapi import Body, FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

app = FastAPI()


class Item(BaseModel):
    name: str
    price: float
    description: str | None = None


class Address(BaseModel):
    city: str
    zip: str


class User(BaseModel):
    name: str
    address: Address


@app.post("/items")
def create_item(item: Item) -> dict:
    return item.model_dump()


@app.post("/users")
def create_user(user: User) -> dict:
    return user.model_dump()


@app.post("/list-body")
def create_list(items: list[Item]) -> dict:
    return {"count": len(items), "names": [i.name for i in items]}


@app.post("/raw-body")
def raw_body(payload: Annotated[dict, Body()]) -> dict:
    return payload


@app.post("/multi-body")
def multi_body(item: Item, user: User) -> dict:
    return {"item": item.model_dump(), "user": user.model_dump()}


@app.post("/embedded")
def embedded(item: Annotated[Item, Body(embed=True)]) -> dict:
    return item.model_dump()


client = TestClient(app)


def test_pydantic_body_happy_path() -> None:
    response = client.post("/items", json={"name": "Foo", "price": 9.99})
    assert response.status_code == 200, response.text
    assert response.json() == {"name": "Foo", "price": 9.99, "description": None}


def test_pydantic_body_with_optional() -> None:
    response = client.post("/items", json={"name": "Foo", "price": 1.0, "description": "Bar"})
    assert response.status_code == 200
    assert response.json()["description"] == "Bar"


def test_pydantic_body_missing_required_field_returns_422() -> None:
    response = client.post("/items", json={"name": "Foo"})  # missing price
    assert response.status_code == 422
    err = response.json()["detail"][0]
    assert err["type"] == "missing"
    assert err["loc"] == ["body", "price"]


def test_pydantic_body_wrong_type_returns_422() -> None:
    response = client.post("/items", json={"name": "Foo", "price": "not-a-number"})
    assert response.status_code == 422
    err = response.json()["detail"][0]
    assert err["loc"] == ["body", "price"]


def test_nested_pydantic_body() -> None:
    response = client.post(
        "/users",
        json={"name": "Alice", "address": {"city": "Kyiv", "zip": "01001"}},
    )
    assert response.status_code == 200
    assert response.json() == {
        "name": "Alice",
        "address": {"city": "Kyiv", "zip": "01001"},
    }


def test_nested_validation_error_loc_includes_inner_field() -> None:
    """Nested validation error `loc` must include the outer field name."""
    response = client.post(
        "/users",
        json={"name": "Alice", "address": {"city": "Kyiv"}},  # missing zip
    )
    assert response.status_code == 422
    err = response.json()["detail"][0]
    assert err["loc"] == ["body", "address", "zip"]


def test_list_body() -> None:
    response = client.post(
        "/list-body",
        json=[
            {"name": "A", "price": 1.0},
            {"name": "B", "price": 2.0},
        ],
    )
    assert response.status_code == 200
    assert response.json() == {"count": 2, "names": ["A", "B"]}


def test_raw_dict_body() -> None:
    response = client.post("/raw-body", json={"any": "thing", "nested": {"x": 1}})
    assert response.status_code == 200
    assert response.json() == {"any": "thing", "nested": {"x": 1}}


def test_multi_body_auto_embeds() -> None:
    """Two body params must be wrapped in {item: ..., user: ...} envelope."""
    response = client.post(
        "/multi-body",
        json={
            "item": {"name": "Foo", "price": 1.0},
            "user": {"name": "Bob", "address": {"city": "K", "zip": "1"}},
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["item"]["name"] == "Foo"
    assert body["user"]["name"] == "Bob"


def test_explicit_embed_wraps_single_body() -> None:
    """`Body(embed=True)` requires the body to be wrapped under the param name."""
    response = client.post("/embedded", json={"item": {"name": "X", "price": 5.0}})
    assert response.status_code == 200
    assert response.json()["name"] == "X"


def test_embed_rejects_unwrapped_body() -> None:
    response = client.post("/embedded", json={"name": "X", "price": 5.0})
    assert response.status_code == 422


def test_empty_body_when_required_returns_422() -> None:
    """POST without a body to a route that requires one is a validation error."""
    response = client.post("/items")
    assert response.status_code == 422
