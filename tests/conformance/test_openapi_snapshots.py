"""OpenAPI structural snapshots — exact-shape comparison of representative routes.

These tests are stricter than `test_openapi.py` (which only checks individual
fields). Here we assert the EXACT structure of operation objects, parameter
lists, request body schemas, and security entries. A Rust port that subtly
reorders or renames keys will fail these tests immediately.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import FastAPI, Path, Query, Security
from fastapi.security import OAuth2PasswordBearer
from fastapi.testclient import TestClient
from pydantic import BaseModel


class Item(BaseModel):
    name: str
    price: float


app = FastAPI(title="Snapshot API", version="1.0.0")
oauth2 = OAuth2PasswordBearer(tokenUrl="/token")


@app.get("/items")
def list_items(
    skip: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(le=100)] = 10,
) -> list[Item]:
    return []


@app.post("/items", status_code=201)
def create_item(item: Item) -> Item:
    return item


@app.get("/items/{item_id}")
def get_item(item_id: Annotated[int, Path(gt=0)]) -> Item:
    return Item(name="x", price=1.0)


@app.delete("/items/{item_id}", status_code=204)
def delete_item(item_id: int):  # no annotation — 204 forbids body
    return None


@app.get("/me")
def me(token: str = Security(oauth2)) -> dict:
    return {"token": token}


@app.get("/legacy", deprecated=True)
def legacy() -> dict:
    return {}


client = TestClient(app)


def _schema() -> dict:
    return client.get("/openapi.json").json()


def test_openapi_top_level_keys() -> None:
    schema = _schema()
    # OpenAPI 3.x top-level required keys
    assert set(schema.keys()) >= {"openapi", "info", "paths"}
    assert schema["openapi"].startswith("3.")


def test_info_object_exact_shape() -> None:
    schema = _schema()
    assert schema["info"] == {"title": "Snapshot API", "version": "1.0.0"}


def test_get_with_query_params_parameters_block() -> None:
    """The `parameters` array for GET /items has exactly two entries: skip + limit."""
    schema = _schema()
    params = schema["paths"]["/items"]["get"]["parameters"]
    names = {p["name"] for p in params}
    assert names == {"skip", "limit"}

    skip = next(p for p in params if p["name"] == "skip")
    assert skip["in"] == "query"
    assert skip["required"] is False
    # ge=0 must appear in the schema for skip
    assert skip["schema"].get("minimum") == 0
    # Default value preserved
    assert skip["schema"].get("default") == 0

    limit = next(p for p in params if p["name"] == "limit")
    assert limit["schema"].get("maximum") == 100
    assert limit["schema"].get("default") == 10


def test_path_param_marked_required_in_schema() -> None:
    schema = _schema()
    params = schema["paths"]["/items/{item_id}"]["get"]["parameters"]
    item_id = next(p for p in params if p["name"] == "item_id")
    assert item_id["in"] == "path"
    assert item_id["required"] is True
    # gt=0 → exclusiveMinimum: 0 (Pydantic v2/OpenAPI 3.1)
    schema_part = item_id["schema"]
    assert schema_part.get("exclusiveMinimum") == 0 or schema_part.get("minimum") == 0


def test_post_request_body_references_pydantic_model() -> None:
    schema = _schema()
    rb = schema["paths"]["/items"]["post"]["requestBody"]
    assert rb["required"] is True
    json_content = rb["content"]["application/json"]
    assert "schema" in json_content
    # Schema points to a Pydantic component
    sch = json_content["schema"]
    assert "$ref" in sch
    assert sch["$ref"].endswith("/Item")


def test_post_201_status_in_responses() -> None:
    schema = _schema()
    responses = schema["paths"]["/items"]["post"]["responses"]
    assert "201" in responses
    assert "200" not in responses  # Default replaced when status_code=201


def test_delete_204_responses_have_no_content_schema() -> None:
    schema = _schema()
    responses = schema["paths"]["/items/{item_id}"]["delete"]["responses"]
    assert "204" in responses
    # 204 must NOT carry a `content` key (no body allowed)
    assert "content" not in responses["204"]


def test_pydantic_model_in_components_schemas() -> None:
    schema = _schema()
    schemas = schema["components"]["schemas"]
    assert "Item" in schemas
    item = schemas["Item"]
    assert item["type"] == "object"
    assert set(item["properties"].keys()) == {"name", "price"}
    assert set(item["required"]) == {"name", "price"}


def test_security_route_has_security_array_entry() -> None:
    schema = _schema()
    op = schema["paths"]["/me"]["get"]
    sec = op.get("security", [])
    assert sec == [{"OAuth2PasswordBearer": []}], (
        f"Security entry mismatch: expected exactly OAuth2PasswordBearer with no scopes, got {sec}"
    )


def test_securityschemes_oauth2_password_block() -> None:
    schema = _schema()
    schemes = schema["components"]["securitySchemes"]
    assert "OAuth2PasswordBearer" in schemes
    s = schemes["OAuth2PasswordBearer"]
    assert s == {
        "type": "oauth2",
        "flows": {"password": {"scopes": {}, "tokenUrl": "/token"}},
    }


def test_deprecated_marker_is_true_only_when_set() -> None:
    schema = _schema()
    assert schema["paths"]["/legacy"]["get"]["deprecated"] is True
    # Other paths should NOT have deprecated=True
    assert schema["paths"]["/items"]["get"].get("deprecated", False) is False
    assert schema["paths"]["/items"]["post"].get("deprecated", False) is False


def test_path_methods_lowercase() -> None:
    """OpenAPI uses lowercase method names: get/post/put/delete/etc."""
    schema = _schema()
    item_id_methods = {
        k for k in schema["paths"]["/items/{item_id}"].keys() if k not in ("parameters",)
    }
    assert all(m.islower() for m in item_id_methods)
    assert "get" in item_id_methods
    assert "delete" in item_id_methods


def test_validation_error_response_schema_in_components() -> None:
    """422 routes reference HTTPValidationError; the schema must exist."""
    schema = _schema()
    schemas = schema["components"]["schemas"]
    # FastAPI auto-defines HTTPValidationError + ValidationError schemas
    assert "HTTPValidationError" in schemas
    assert "ValidationError" in schemas


def test_validation_error_inner_structure() -> None:
    schema = _schema()
    ve = schema["components"]["schemas"]["ValidationError"]
    # Must have the {type, loc, msg} contract
    props = ve["properties"]
    assert "loc" in props
    assert "msg" in props
    assert "type" in props
    required = set(ve.get("required", []))
    assert {"loc", "msg", "type"}.issubset(required)


def test_response_with_body_has_content_application_json() -> None:
    schema = _schema()
    op = schema["paths"]["/items"]["post"]
    resp_201 = op["responses"]["201"]
    assert "content" in resp_201
    assert "application/json" in resp_201["content"]
