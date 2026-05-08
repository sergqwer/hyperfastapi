"""response_model_exclude_unset / exclude_none / exclude_defaults — separately and combined."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel


class Item(BaseModel):
    name: str
    description: str | None = None
    price: float = 0.0
    tax: float | None = None


app = FastAPI()


@app.get("/all-fields", response_model=Item)
def all_fields() -> Item:
    """Default — all fields appear, including None."""
    return Item(name="Foo")


@app.get("/exclude-unset", response_model=Item, response_model_exclude_unset=True)
def excl_unset() -> Item:
    return Item(name="Foo")


@app.get("/exclude-unset-some-set", response_model=Item, response_model_exclude_unset=True)
def excl_unset_some() -> Item:
    return Item(name="Foo", description="hello")


@app.get("/exclude-none", response_model=Item, response_model_exclude_none=True)
def excl_none() -> dict:
    return {"name": "Foo", "description": None, "price": 1.0, "tax": None}


@app.get("/exclude-defaults", response_model=Item, response_model_exclude_defaults=True)
def excl_defaults() -> Item:
    return Item(name="Foo", price=0.0)


@app.get(
    "/all-three",
    response_model=Item,
    response_model_exclude_unset=True,
    response_model_exclude_none=True,
    response_model_exclude_defaults=True,
)
def all_three() -> Item:
    return Item(name="Foo", description=None, price=0.0)


client = TestClient(app)


def test_default_all_fields_present_with_none() -> None:
    """No exclusion → every field, with None for unset Optional[X]."""
    response = client.get("/all-fields")
    body = response.json()
    assert body == {"name": "Foo", "description": None, "price": 0.0, "tax": None}


def test_exclude_unset_drops_never_assigned_fields() -> None:
    response = client.get("/exclude-unset")
    body = response.json()
    assert body == {"name": "Foo"}


def test_exclude_unset_keeps_explicitly_set() -> None:
    response = client.get("/exclude-unset-some-set")
    body = response.json()
    assert body == {"name": "Foo", "description": "hello"}


def test_exclude_none_drops_only_none_values() -> None:
    """Note: even though `description` was explicitly set to None, exclude_none drops it."""
    response = client.get("/exclude-none")
    body = response.json()
    assert "description" not in body
    assert "tax" not in body
    assert body["name"] == "Foo"
    assert body["price"] == 1.0


def test_exclude_defaults_drops_default_valued_fields() -> None:
    """price=0.0 is default → dropped. name has no default → kept."""
    response = client.get("/exclude-defaults")
    body = response.json()
    assert body == {"name": "Foo"}


def test_all_three_combined_minimal_output() -> None:
    """All three flags combined → only explicitly-set non-None non-default fields."""
    response = client.get("/all-three")
    body = response.json()
    # Only name remains: description=None (excluded), price=0.0 (default)
    assert body == {"name": "Foo"}


def test_exclude_unset_with_optional_set_to_none() -> None:
    """Setting Optional to None DOES count as 'set' — exclude_unset keeps it."""
    test_app = FastAPI()

    @test_app.get("/r", response_model=Item, response_model_exclude_unset=True)
    def r() -> Item:
        return Item(name="Foo", description=None)

    response = TestClient(test_app).get("/r")
    body = response.json()
    # description was explicitly assigned (to None), so exclude_unset keeps it
    assert body == {"name": "Foo", "description": None}
