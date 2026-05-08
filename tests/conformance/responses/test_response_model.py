"""response_model — filtering, exclude_unset, exclude_none, exclude_defaults, include/exclude."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel


class Item(BaseModel):
    name: str
    description: str | None = None
    price: float = 0.0
    tax: float | None = None


class PrivateItem(BaseModel):
    name: str
    price: float


class Inner(BaseModel):
    foo: str
    bar: str


class Outer(BaseModel):
    ref: Inner
    baz: str


app = FastAPI()


@app.get("/strict-public", response_model=PrivateItem)
def strict_public() -> dict:
    return {"name": "Foo", "price": 9.99, "internal": "secret"}


@app.get("/exclude-unset", response_model=Item, response_model_exclude_unset=True)
def excl_unset() -> Item:
    return Item(name="Foo")  # description, price, tax not set


@app.get("/exclude-none", response_model=Item, response_model_exclude_none=True)
def excl_none() -> dict:
    return {"name": "Foo", "description": None, "price": 1.0, "tax": None}


@app.get("/exclude-defaults", response_model=Item, response_model_exclude_defaults=True)
def excl_defaults() -> Item:
    return Item(name="Foo", price=0.0)  # price=0.0 is the default


@app.get("/include-only", response_model=Outer, response_model_include={"baz": ..., "ref": {"foo"}})
def include_only() -> Outer:
    return Outer(ref=Inner(foo="F", bar="B"), baz="Z")


@app.get(
    "/exclude-fields",
    response_model=Outer,
    response_model_exclude={"ref": {"bar"}},
)
def exclude_fields() -> Outer:
    return Outer(ref=Inner(foo="F", bar="B"), baz="Z")


client = TestClient(app)


def test_response_model_filters_unrelated_fields() -> None:
    """Fields not declared in response_model must NOT appear in the JSON."""
    response = client.get("/strict-public")
    assert response.status_code == 200
    body = response.json()
    assert body == {"name": "Foo", "price": 9.99}
    assert "internal" not in body


def test_exclude_unset_omits_fields_never_assigned() -> None:
    """exclude_unset=True: only explicitly-set fields appear in the response."""
    response = client.get("/exclude-unset")
    assert response.status_code == 200
    body = response.json()
    assert body == {"name": "Foo"}


def test_exclude_none_omits_null_fields() -> None:
    """exclude_none=True: fields whose value is None are dropped."""
    response = client.get("/exclude-none")
    assert response.status_code == 200
    body = response.json()
    assert body == {"name": "Foo", "price": 1.0}
    assert "description" not in body
    assert "tax" not in body


def test_exclude_defaults_omits_default_values() -> None:
    """exclude_defaults=True: fields equal to their schema default are dropped."""
    response = client.get("/exclude-defaults")
    assert response.status_code == 200
    body = response.json()
    # name was not at default ("Foo" vs no default), price=0.0 is default
    assert body == {"name": "Foo"}


def test_response_model_include_picks_specific_fields() -> None:
    """include={...}: only listed fields (and nested) appear."""
    response = client.get("/include-only")
    assert response.status_code == 200
    body = response.json()
    assert body == {"baz": "Z", "ref": {"foo": "F"}}
    # bar excluded via include only listing 'foo' for the nested model
    assert "bar" not in body["ref"]


def test_response_model_exclude_drops_specific_fields() -> None:
    """exclude={...}: listed fields are dropped from output, others kept."""
    response = client.get("/exclude-fields")
    assert response.status_code == 200
    body = response.json()
    assert body == {"baz": "Z", "ref": {"foo": "F"}}
    assert "bar" not in body["ref"]


def test_response_model_in_openapi_components() -> None:
    """response_model class must appear in OpenAPI components.schemas."""
    schema = client.get("/openapi.json").json()
    schemas = schema.get("components", {}).get("schemas", {})
    assert "Item" in schemas or "PrivateItem" in schemas, (
        f"Expected response models in components.schemas, got: {list(schemas)}"
    )
