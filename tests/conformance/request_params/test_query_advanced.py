"""Advanced Query() — deprecated, examples, regex, openapi extras."""

from __future__ import annotations

from typing import Annotated

from fastapi import FastAPI, Query
from fastapi.testclient import TestClient

app = FastAPI()


@app.get("/deprecated")
def deprecated_q(q: Annotated[str | None, Query(deprecated=True)] = None) -> dict:
    return {"q": q}


@app.get("/with-example")
def with_example(
    q: Annotated[str, Query(example="hello", description="A greeting")] = "default",
) -> dict:
    return {"q": q}


@app.get("/with-openapi-examples")
def with_openapi_examples(
    q: Annotated[
        str,
        Query(
            openapi_examples={
                "greeting": {"summary": "A greeting", "value": "hello"},
                "wave": {"summary": "A wave", "value": "hi"},
            },
        ),
    ] = "default",
) -> dict:
    return {"q": q}


@app.get("/regex")
def regex_q(code: Annotated[str, Query(pattern=r"^[A-Z]{3}-\d{4}$")]) -> dict:
    return {"code": code}


@app.get("/exclude-from-schema")
def excluded_q(internal: Annotated[str | None, Query(include_in_schema=False)] = None) -> dict:
    return {"internal": internal}


@app.get("/title-and-description")
def titled(
    q: Annotated[
        str | None, Query(title="My Query", description="Useful query param")
    ] = None,
) -> dict:
    return {"q": q}


client = TestClient(app)


def test_deprecated_query_still_works_at_runtime() -> None:
    response = client.get("/deprecated?q=foo")
    assert response.status_code == 200
    assert response.json() == {"q": "foo"}


def test_deprecated_query_marker_in_openapi() -> None:
    schema = client.get("/openapi.json").json()
    op = schema["paths"]["/deprecated"]["get"]
    q_param = next(p for p in op["parameters"] if p["name"] == "q")
    assert q_param.get("deprecated") is True


def test_query_example_appears_in_openapi() -> None:
    schema = client.get("/openapi.json").json()
    op = schema["paths"]["/with-example"]["get"]
    q_param = next(p for p in op["parameters"] if p["name"] == "q")
    # 'example' may live on schema or top-level — accept either
    assert "hello" in str(q_param), f"Example missing from schema: {q_param}"


def test_query_openapi_examples_dict_in_schema() -> None:
    schema = client.get("/openapi.json").json()
    op = schema["paths"]["/with-openapi-examples"]["get"]
    q_param = next(p for p in op["parameters"] if p["name"] == "q")
    examples = q_param.get("examples", {})
    assert "greeting" in examples
    assert examples["greeting"]["value"] == "hello"


def test_query_regex_pattern_validates() -> None:
    response = client.get("/regex?code=ABC-1234")
    assert response.status_code == 200
    assert response.json() == {"code": "ABC-1234"}


def test_query_regex_pattern_rejects_mismatched() -> None:
    response = client.get("/regex?code=lowercase-123")
    assert response.status_code == 422
    err = response.json()["detail"][0]
    assert err["loc"] == ["query", "code"]


def test_query_include_in_schema_false_hides_from_openapi() -> None:
    schema = client.get("/openapi.json").json()
    op = schema["paths"]["/exclude-from-schema"]["get"]
    params = op.get("parameters", [])
    assert not any(p["name"] == "internal" for p in params)


def test_query_include_in_schema_false_still_works_at_runtime() -> None:
    response = client.get("/exclude-from-schema?internal=secret")
    assert response.status_code == 200
    assert response.json() == {"internal": "secret"}


def test_query_title_and_description_in_openapi() -> None:
    schema = client.get("/openapi.json").json()
    op = schema["paths"]["/title-and-description"]["get"]
    q_param = next(p for p in op["parameters"] if p["name"] == "q")
    assert q_param.get("description") == "Useful query param"
    # Title may be on schema or param
    assert "My Query" in str(q_param)
