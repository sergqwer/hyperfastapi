"""OpenAPI schema generation — paths, parameters, components, deprecated, tags, summary."""

from __future__ import annotations

from fastapi import FastAPI, Query
from fastapi.testclient import TestClient
from pydantic import BaseModel


class Item(BaseModel):
    name: str
    price: float


app = FastAPI(title="Test API", version="1.2.3", description="Hello", summary="Brief")


@app.get("/items", tags=["items"], summary="List items", description="Return all items.")
def list_items(q: str | None = Query(None)) -> list[Item]:
    return []


@app.post("/items", tags=["items"], status_code=201)
def create_item(item: Item) -> Item:
    return item


@app.get("/legacy", deprecated=True)
def legacy() -> dict:
    return {}


@app.get("/items/{item_id}", tags=["items"])
def get_item(item_id: int) -> Item:
    return Item(name="x", price=1.0)


client = TestClient(app)


def test_openapi_json_endpoint_present() -> None:
    response = client.get("/openapi.json")
    assert response.status_code == 200
    schema = response.json()
    assert "paths" in schema
    assert "components" in schema
    assert "info" in schema


def test_openapi_info_reflects_app_metadata() -> None:
    schema = client.get("/openapi.json").json()
    assert schema["info"]["title"] == "Test API"
    assert schema["info"]["version"] == "1.2.3"
    assert schema["info"]["description"] == "Hello"
    assert schema["info"]["summary"] == "Brief"


def test_openapi_paths_include_all_routes() -> None:
    schema = client.get("/openapi.json").json()
    paths = schema["paths"]
    assert "/items" in paths
    assert "/items/{item_id}" in paths
    assert "/legacy" in paths
    # Method keys
    assert "get" in paths["/items"]
    assert "post" in paths["/items"]


def test_openapi_query_parameter_recorded() -> None:
    schema = client.get("/openapi.json").json()
    op = schema["paths"]["/items"]["get"]
    params = op.get("parameters", [])
    q_param = next((p for p in params if p["name"] == "q"), None)
    assert q_param is not None, f"q parameter missing from /items GET, got {params}"
    assert q_param["in"] == "query"
    assert q_param["required"] is False


def test_openapi_path_parameter_required() -> None:
    schema = client.get("/openapi.json").json()
    op = schema["paths"]["/items/{item_id}"]["get"]
    params = op["parameters"]
    item_id = next(p for p in params if p["name"] == "item_id")
    assert item_id["in"] == "path"
    assert item_id["required"] is True


def test_openapi_tags_propagated() -> None:
    schema = client.get("/openapi.json").json()
    assert "items" in schema["paths"]["/items"]["get"]["tags"]


def test_openapi_summary_and_description() -> None:
    schema = client.get("/openapi.json").json()
    op = schema["paths"]["/items"]["get"]
    assert op["summary"] == "List items"
    assert op["description"] == "Return all items."


def test_openapi_deprecated_marker() -> None:
    schema = client.get("/openapi.json").json()
    assert schema["paths"]["/legacy"]["get"]["deprecated"] is True


def test_openapi_status_code_201_present() -> None:
    schema = client.get("/openapi.json").json()
    op = schema["paths"]["/items"]["post"]
    assert "201" in op["responses"]


def test_openapi_pydantic_model_in_components() -> None:
    schema = client.get("/openapi.json").json()
    schemas = schema["components"]["schemas"]
    assert "Item" in schemas
    item_schema = schemas["Item"]
    assert item_schema["type"] == "object"
    assert "name" in item_schema["properties"]
    assert "price" in item_schema["properties"]
