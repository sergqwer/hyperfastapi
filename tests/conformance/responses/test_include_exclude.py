"""response_model_include / response_model_exclude — set, dict, nested specifications."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel


class Inner(BaseModel):
    foo: str
    bar: str
    baz: str


class Outer(BaseModel):
    name: str
    description: str
    inner: Inner
    tags: list[str]


app = FastAPI()


def make_outer() -> Outer:
    return Outer(
        name="N",
        description="D",
        inner=Inner(foo="F", bar="B", baz="Z"),
        tags=["t1", "t2"],
    )


# Set-based include: top-level fields only
@app.get(
    "/include-set",
    response_model=Outer,
    response_model_include={"name", "description"},
)
def include_set() -> Outer:
    return make_outer()


# Set-based exclude
@app.get(
    "/exclude-set",
    response_model=Outer,
    response_model_exclude={"description", "tags"},
)
def exclude_set() -> Outer:
    return make_outer()


# Dict-based include: nested field selection
@app.get(
    "/include-nested",
    response_model=Outer,
    response_model_include={"name": ..., "inner": {"foo"}},
)
def include_nested() -> Outer:
    return make_outer()


# Dict-based exclude: drop one nested field
@app.get(
    "/exclude-nested",
    response_model=Outer,
    response_model_exclude={"inner": {"bar", "baz"}},
)
def exclude_nested() -> Outer:
    return make_outer()


# Both include and exclude — exclude wins
@app.get(
    "/include-and-exclude",
    response_model=Outer,
    response_model_include={"name", "description", "inner"},
    response_model_exclude={"description"},
)
def include_and_exclude() -> Outer:
    return make_outer()


# Aliased model — by_alias=True for response
class AliasedItem(BaseModel):
    name: str
    item_type: str

    model_config = {"populate_by_name": True}


@app.get("/by-alias", response_model=AliasedItem, response_model_by_alias=True)
def by_alias() -> AliasedItem:
    return AliasedItem(name="X", item_type="t")


# Empty include results in empty object
@app.get(
    "/include-empty",
    response_model=Outer,
    response_model_include=set(),
)
def include_empty() -> Outer:
    return make_outer()


client = TestClient(app)


def test_include_set_keeps_only_listed_fields() -> None:
    response = client.get("/include-set")
    assert response.status_code == 200
    assert response.json() == {"name": "N", "description": "D"}


def test_exclude_set_drops_listed_fields() -> None:
    response = client.get("/exclude-set")
    body = response.json()
    assert "description" not in body
    assert "tags" not in body
    assert body["name"] == "N"
    assert body["inner"] == {"foo": "F", "bar": "B", "baz": "Z"}


def test_include_dict_with_nested_set() -> None:
    """include={'name': ..., 'inner': {'foo'}} — keep name fully + only inner.foo."""
    response = client.get("/include-nested")
    body = response.json()
    assert body == {"name": "N", "inner": {"foo": "F"}}


def test_exclude_dict_with_nested_set_drops_subfields() -> None:
    response = client.get("/exclude-nested")
    body = response.json()
    assert body["inner"] == {"foo": "F"}
    assert "bar" not in body["inner"]
    assert "baz" not in body["inner"]


def test_include_and_exclude_combination() -> None:
    """When both are set, the result is `include - exclude`."""
    response = client.get("/include-and-exclude")
    body = response.json()
    # description was in include but explicitly excluded
    assert "description" not in body
    assert "name" in body
    assert "inner" in body


def test_include_empty_yields_empty_object() -> None:
    response = client.get("/include-empty")
    body = response.json()
    assert body == {}


def test_response_model_by_alias_default_uses_python_names() -> None:
    """Without by_alias, fields use Python names (no alias yet for AliasedItem)."""
    response = client.get("/by-alias")
    assert response.status_code == 200
    body = response.json()
    assert "name" in body
    assert "item_type" in body
