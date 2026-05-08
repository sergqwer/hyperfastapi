"""OpenAPI extras — examples, additional_responses, operation_id, include_in_schema."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel


class Item(BaseModel):
    name: str
    price: float


class ErrorMessage(BaseModel):
    code: str
    message: str


app = FastAPI()


@app.get(
    "/items",
    operation_id="listItems",
    responses={
        404: {"model": ErrorMessage, "description": "No items found"},
        500: {"description": "Internal Server Error"},
    },
)
def list_items() -> list[Item]:
    return []


@app.post(
    "/items",
    summary="Create one item",
    description="Create an item with a name and price.",
    response_description="The created item",
    deprecated=False,
)
def create_item(item: Item) -> Item:
    return item


@app.get("/internal", include_in_schema=False)
def internal() -> dict:
    return {"hidden": True}


@app.get("/legacy", deprecated=True)
def legacy() -> dict:
    return {}


client = TestClient(app)


def test_custom_operation_id_in_schema() -> None:
    schema = client.get("/openapi.json").json()
    op = schema["paths"]["/items"]["get"]
    assert op["operationId"] == "listItems"


def test_additional_responses_in_schema() -> None:
    schema = client.get("/openapi.json").json()
    responses = schema["paths"]["/items"]["get"]["responses"]
    assert "404" in responses
    assert "500" in responses
    # 404 has a model reference
    content_404 = responses["404"]["content"]
    assert "application/json" in content_404


def test_response_description_in_schema() -> None:
    schema = client.get("/openapi.json").json()
    op = schema["paths"]["/items"]["post"]
    # Default success response gets the response_description
    success = op["responses"]["200"]
    assert success["description"] == "The created item"


def test_summary_and_description() -> None:
    schema = client.get("/openapi.json").json()
    op = schema["paths"]["/items"]["post"]
    assert op["summary"] == "Create one item"
    assert "name and price" in op["description"]


def test_include_in_schema_false_hides_route() -> None:
    schema = client.get("/openapi.json").json()
    assert "/internal" not in schema["paths"]
    # But the route still works at runtime
    response = client.get("/internal")
    assert response.status_code == 200
    assert response.json() == {"hidden": True}


def test_deprecated_marker_in_schema() -> None:
    schema = client.get("/openapi.json").json()
    op = schema["paths"]["/legacy"]["get"]
    assert op["deprecated"] is True


def test_non_deprecated_route_lacks_deprecated_marker() -> None:
    """A normal route either omits 'deprecated' or sets it to False — never True."""
    schema = client.get("/openapi.json").json()
    op = schema["paths"]["/items"]["post"]
    assert op.get("deprecated", False) is False


def test_pydantic_model_appears_in_components() -> None:
    schema = client.get("/openapi.json").json()
    schemas = schema["components"]["schemas"]
    assert "Item" in schemas
    assert "ErrorMessage" in schemas


def test_path_methods_use_lowercase() -> None:
    """OpenAPI methods are lowercase: get, post, etc."""
    schema = client.get("/openapi.json").json()
    methods = schema["paths"]["/items"].keys()
    assert all(m.islower() for m in methods if m not in ("parameters",))
