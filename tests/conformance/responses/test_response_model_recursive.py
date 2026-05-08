"""Recursive Pydantic models — self-references, list of self, mutual recursion."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel


class TreeNode(BaseModel):
    """Self-referential: a node can have children of the same type."""

    name: str
    children: list["TreeNode"] = []


TreeNode.model_rebuild()


class Person(BaseModel):
    """Mutual recursion: Person ↔ Address (both via forward ref)."""

    name: str
    address: "Address | None" = None


class Address(BaseModel):
    street: str
    resident: Person | None = None


Person.model_rebuild()
Address.model_rebuild()


app = FastAPI()


@app.get("/tree", response_model=TreeNode)
def get_tree() -> TreeNode:
    return TreeNode(
        name="root",
        children=[
            TreeNode(name="left", children=[TreeNode(name="left-leaf")]),
            TreeNode(name="right"),
        ],
    )


@app.get("/person", response_model=Person)
def get_person() -> Person:
    return Person(
        name="Alice",
        address=Address(
            street="Main",
            resident=Person(name="Alice"),  # Cycle would loop; we keep depth bounded.
        ),
    )


client = TestClient(app)


def test_self_referential_tree_serializes() -> None:
    response = client.get("/tree")
    assert response.status_code == 200
    body = response.json()
    assert body == {
        "name": "root",
        "children": [
            {
                "name": "left",
                "children": [{"name": "left-leaf", "children": []}],
            },
            {"name": "right", "children": []},
        ],
    }


def test_self_referential_in_openapi_uses_ref() -> None:
    """A self-referential model should use $ref to itself in OpenAPI components."""
    schema = client.get("/openapi.json").json()
    tree_schema = schema["components"]["schemas"]["TreeNode"]
    children_prop = tree_schema["properties"]["children"]
    # The items of `children` reference TreeNode itself
    items = children_prop["items"]
    assert "$ref" in items or "TreeNode" in str(items)


def test_mutual_recursion_serializes_two_levels() -> None:
    response = client.get("/person")
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Alice"
    assert body["address"]["street"] == "Main"
    assert body["address"]["resident"]["name"] == "Alice"


def test_mutual_recursion_in_openapi_components() -> None:
    schema = client.get("/openapi.json").json()
    schemas = schema["components"]["schemas"]
    assert "Person" in schemas
    assert "Address" in schemas


def test_deeply_nested_tree_preserves_structure() -> None:
    """Construct a 5-level deep tree and verify all levels are serialized."""
    deep_app = FastAPI()

    @deep_app.get("/deep", response_model=TreeNode)
    def deep() -> TreeNode:
        node = TreeNode(name="lvl-5")
        for i in range(4, -1, -1):
            node = TreeNode(name=f"lvl-{i}", children=[node])
        return node

    response = TestClient(deep_app).get("/deep")
    body = response.json()
    # Walk down 6 levels
    cur = body
    for i in range(6):
        assert cur["name"] == f"lvl-{i}"
        if i < 5:
            assert len(cur["children"]) == 1
            cur = cur["children"][0]
        else:
            assert cur["children"] == []
