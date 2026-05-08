"""Type-fidelity invariants — explicit checks that values come back as the RIGHT TYPES.

The upstream FastAPI suite leans on `response.json()`, which silently parses
anything that vaguely looks like JSON. For the Rust port we need stricter
guarantees: these tests catch regressions like
- handler returns a dict but we serialize via `str(d)` → `"{'foo': 'bar'}"` with single quotes
- response missing or wrong `content-type` header
- a 204 response that still ships a body
- `Pydantic` `Optional=None` silently dropped vs. emitted as JSON `null`
- UTF-8 BOM sneaking in from an over-helpful encoder
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI, status
from fastapi.testclient import TestClient
from pydantic import BaseModel


pytestmark = pytest.mark.type_fidelity


app = FastAPI()


@app.get("/json-dict")
def json_dict() -> dict:
    return {"foo": "bar", "n": 1, "ok": True}


@app.get("/no-content-with-dict", status_code=status.HTTP_204_NO_CONTENT)
def no_content_with_dict():  # noqa: ANN201  — no annotation deliberately
    # Intentional: handler returns a dict but status_code=204 forbids a body.
    # FastAPI must drop the body at runtime. Note: declaring a `-> dict` return
    # type would fail at registration time (FastAPI asserts on body-allowed
    # status codes), which is a strictly stronger guarantee — covered by the
    # separate `test_204_rejects_dict_return_annotation` test below.
    return {"this": "should not appear"}


class Item(BaseModel):
    name: str
    description: str | None = None


@app.get("/optional-none", response_model=Item)
def optional_none() -> dict:
    return {"name": "Foo", "description": None}


client = TestClient(app)


def test_content_type_is_exactly_application_json() -> None:
    """JSON RFC 8259 mandates UTF-8, so charset is redundant. Starlette/FastAPI
    emit a bare `application/json` — verify exactly that.
    """
    response = client.get("/json-dict")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/json", (
        f"Expected exactly 'application/json', got {response.headers['content-type']!r}"
    )


def test_json_body_uses_double_quotes_not_python_repr() -> None:
    """`response.content` is bytes; decoded UTF-8 is valid JSON with double quotes.

    The classic regression: serializing via `str(some_dict)` instead of
    `json.dumps(...)` produces `b"{'foo': 'bar'}"` — looks JSON-ish but uses
    single quotes and won't parse with `JSON.parse`. This test fails fast on
    that mistake.
    """
    response = client.get("/json-dict")

    assert isinstance(response.content, bytes), (
        f"response.content expected bytes, got {type(response.content).__name__}"
    )

    text = response.content.decode("utf-8")
    parsed = json.loads(text)  # raises if it isn't valid JSON
    assert parsed == {"foo": "bar", "n": 1, "ok": True}

    # Single quotes around keys/values are the smoking gun for Python repr
    repr_form = str({"foo": "bar", "n": 1, "ok": True})
    assert text != repr_form, (
        f"Body matches Python repr() output (single quotes, etc.): {text!r}"
    )


def test_json_body_has_no_utf8_bom() -> None:
    """A response body must not start with UTF-8 BOM (EF BB BF).

    Some encoders helpfully prepend a BOM; many JSON parsers (browsers,
    `JSON.parse`, even `json.loads`) reject it. Standard JSON-over-HTTP must
    be raw UTF-8 with no BOM.
    """
    response = client.get("/json-dict")
    assert not response.content.startswith(b"\xef\xbb\xbf"), (
        f"Response body starts with UTF-8 BOM: {response.content[:6]!r}"
    )


def test_204_has_empty_body_even_if_handler_returns_dict() -> None:
    """`status_code=204` → empty body at runtime, regardless of what the handler returned.

    HTTP/1.1 forbids a body for 204; serializing one would confuse some clients
    (Content-Length mismatch). FastAPI must silently drop the dict at runtime.
    """
    response = client.get("/no-content-with-dict")
    assert response.status_code == 204
    assert response.content == b"", (
        f"204 response must have empty body, got {response.content!r}"
    )

    # Content-Length, if present, must be 0
    cl = response.headers.get("content-length")
    if cl is not None:
        assert cl == "0", f"204 with content-length={cl!r}, expected '0' or absent"


def test_204_rejects_dict_return_annotation_at_registration() -> None:
    """FastAPI's strongest guarantee: a 204 endpoint with a body-typed return
    annotation is rejected at `add_api_route()` time — you can't even start
    the server in this misconfigured state. This is belt-and-suspenders to the
    runtime drop tested above.
    """
    bad_app = FastAPI()
    with pytest.raises(AssertionError, match="204"):

        @bad_app.get("/bad", status_code=status.HTTP_204_NO_CONTENT)
        def bad() -> dict:
            return {"x": 1}


def test_optional_none_serialized_as_null_not_absent() -> None:
    """Pydantic `Optional[X] = None` must appear in the JSON as `"field": null`,
    NOT be silently dropped.

    Without `response_model_exclude_none=True`, the contract is: every field
    present in the model is present in the response, with `null` for None.
    A Rust port that drops nulls during serialization breaks this — the test
    catches that both at the parsed level (key present, value is None) and at
    the raw bytes level (literal 'null' in the body).
    """
    response = client.get("/optional-none")
    assert response.status_code == 200, response.text

    text = response.content.decode("utf-8")
    parsed = json.loads(text)

    assert "description" in parsed, (
        f"`description` key absent from JSON; expected null, got {parsed!r}"
    )
    assert parsed["description"] is None
    assert "null" in text, (
        f"Raw body must contain literal 'null' for the None field: {text!r}"
    )
