"""Validation error type/message details for various field constraints."""

from __future__ import annotations

from typing import Annotated

from fastapi import FastAPI, Query
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field

app = FastAPI()


class NestedItem(BaseModel):
    name: str
    qty: int = Field(..., gt=0)


class Order(BaseModel):
    items: list[NestedItem]
    customer: str = Field(..., min_length=2)


@app.post("/order")
def create_order(order: Order) -> dict:
    return {"items": len(order.items), "customer": order.customer}


@app.get("/q")
def q_route(
    n: Annotated[int, Query(ge=0)],
    s: Annotated[str, Query(min_length=3)],
) -> dict:
    return {"n": n, "s": s}


client = TestClient(app)


def test_validation_error_type_int_parsing() -> None:
    response = client.get("/q?n=abc&s=hello")
    assert response.status_code == 422
    err = response.json()["detail"][0]
    assert err["type"] == "int_parsing"


def test_validation_error_type_greater_than_or_equal() -> None:
    response = client.get("/q?n=-1&s=hello")
    assert response.status_code == 422
    err = response.json()["detail"][0]
    # Pydantic v2 type for ge: 'greater_than_equal'
    assert "greater_than" in err["type"] or "ge" in err["type"]


def test_validation_error_type_string_too_short() -> None:
    response = client.get("/q?n=1&s=ab")
    assert response.status_code == 422
    err = response.json()["detail"][0]
    assert "string_too_short" in err["type"] or "too_short" in err["type"]


def test_validation_error_loc_for_list_item_field() -> None:
    """Errors inside a list element include the index in loc: ['body', 'items', 0, 'qty']."""
    response = client.post(
        "/order",
        json={
            "items": [{"name": "good", "qty": 5}, {"name": "bad", "qty": 0}],
            "customer": "alice",
        },
    )
    assert response.status_code == 422
    err = response.json()["detail"][0]
    assert err["loc"] == ["body", "items", 1, "qty"]


def test_multiple_errors_distinct_locations() -> None:
    """Multiple bad fields → multiple errors, each with its own loc."""
    response = client.post(
        "/order",
        json={"items": [{"name": "a", "qty": 0}], "customer": "x"},  # x too short, qty bad
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    locs = [tuple(e["loc"]) for e in detail]
    assert ("body", "items", 0, "qty") in locs
    assert ("body", "customer") in locs


def test_validation_error_msg_is_human_readable() -> None:
    response = client.get("/q?n=abc&s=hello")
    err = response.json()["detail"][0]
    assert isinstance(err["msg"], str)
    assert len(err["msg"]) > 0


def test_validation_error_input_includes_offending_value() -> None:
    """The 'input' field shows what was actually received."""
    response = client.get("/q?n=abc&s=hello")
    err = response.json()["detail"][0]
    assert err["input"] == "abc"


def test_validation_error_response_is_422_not_400() -> None:
    """Pydantic validation must produce 422, not 400 (RFC 4918 'Unprocessable Entity')."""
    response = client.post("/order", json={"items": []})  # missing customer
    assert response.status_code == 422
