"""Type-fidelity invariants — explicit checks that values come back as the RIGHT TYPES.

The upstream FastAPI suite leans on `response.json()`, which silently parses
anything that vaguely looks like JSON. For the Rust port we need stricter
guarantees: these tests catch regressions like
- handler returns a dict but we serialize via `str(d)` → `"{'foo': 'bar'}"` with single quotes
- response missing or wrong `content-type` header
- a 204 response that still ships a body
- `Pydantic` `Optional=None` silently dropped vs. emitted as JSON `null`
- UTF-8 BOM sneaking in from an over-helpful encoder
- `True`/`False` Python literals leaking out (must be lowercase JSON `true`/`false`)
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI, status
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.testclient import TestClient
from pydantic import BaseModel


pytestmark = pytest.mark.type_fidelity


app = FastAPI()


@app.get("/json-dict")
def json_dict() -> dict:
    return {"foo": "bar", "n": 1, "ok": True}


@app.get("/json-list")
def json_list() -> list:
    return [1, 2, 3]


@app.get("/json-empty-dict")
def json_empty_dict() -> dict:
    return {}


@app.get("/json-empty-list")
def json_empty_list() -> list:
    return []


@app.get("/json-bools")
def json_bools() -> dict:
    return {"yes": True, "no": False}


@app.get("/json-numbers")
def json_numbers() -> dict:
    return {"int": 42, "float": 3.14, "neg": -7, "zero": 0}


@app.get("/json-unicode")
def json_unicode() -> dict:
    return {"hello": "Привіт", "emoji": "🎉"}


@app.get("/no-content-with-dict", status_code=status.HTTP_204_NO_CONTENT)
def no_content_with_dict():  # noqa: ANN201  — no annotation deliberately
    return {"this": "should not appear"}


@app.get("/created", status_code=status.HTTP_201_CREATED)
def created() -> dict:
    return {"id": 42}


class Item(BaseModel):
    name: str
    description: str | None = None


@app.get("/optional-none", response_model=Item)
def optional_none() -> dict:
    return {"name": "Foo", "description": None}


@app.get("/html", response_class=HTMLResponse)
def html() -> str:
    return "<h1>Hello</h1>"


@app.get("/plain", response_class=PlainTextResponse)
def plain() -> str:
    return "just text"


@app.get("/redirect")
def redirect() -> RedirectResponse:
    return RedirectResponse(url="/json-dict")


@app.get("/redirect-308")
def redirect_perm() -> RedirectResponse:
    return RedirectResponse(url="/json-dict", status_code=308)


@app.get("/stream")
def stream() -> StreamingResponse:
    def gen():
        for chunk in (b"part-1 ", b"part-2 ", b"part-3"):
            yield chunk

    return StreamingResponse(gen(), media_type="text/plain")


@app.get("/explicit-headers")
def explicit_headers() -> JSONResponse:
    return JSONResponse(
        content={"k": "v"},
        headers={"X-Trace": "abc-123", "X-Custom": "yes"},
    )


client = TestClient(app)


# ---------------------------------------------------------------------------
# Original 6 invariants
# ---------------------------------------------------------------------------


def test_content_type_is_exactly_application_json() -> None:
    """JSON RFC 8259 mandates UTF-8, so charset is redundant. FastAPI emits bare type."""
    response = client.get("/json-dict")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/json"


def test_json_body_uses_double_quotes_not_python_repr() -> None:
    """Body must be valid JSON with double quotes — never Python `repr()` output."""
    response = client.get("/json-dict")
    assert isinstance(response.content, bytes)
    text = response.content.decode("utf-8")
    parsed = json.loads(text)
    assert parsed == {"foo": "bar", "n": 1, "ok": True}
    repr_form = str({"foo": "bar", "n": 1, "ok": True})
    assert text != repr_form


def test_json_body_has_no_utf8_bom() -> None:
    response = client.get("/json-dict")
    assert not response.content.startswith(b"\xef\xbb\xbf")


def test_204_has_empty_body_even_if_handler_returns_dict() -> None:
    response = client.get("/no-content-with-dict")
    assert response.status_code == 204
    assert response.content == b""
    cl = response.headers.get("content-length")
    if cl is not None:
        assert cl == "0"


def test_optional_none_serialized_as_null_not_absent() -> None:
    response = client.get("/optional-none")
    assert response.status_code == 200
    text = response.content.decode("utf-8")
    parsed = json.loads(text)
    assert "description" in parsed
    assert parsed["description"] is None
    assert "null" in text


def test_204_rejects_dict_return_annotation_at_registration() -> None:
    """Belt-and-suspenders: registration-time assertion catches the misconfiguration."""
    bad_app = FastAPI()
    with pytest.raises(AssertionError, match="204"):

        @bad_app.get("/bad", status_code=status.HTTP_204_NO_CONTENT)
        def bad() -> dict:
            return {"x": 1}


# ---------------------------------------------------------------------------
# Boolean / number / unicode literal fidelity
# ---------------------------------------------------------------------------


def test_json_booleans_are_lowercase() -> None:
    """JSON requires lowercase `true`/`false` — never Python `True`/`False`."""
    response = client.get("/json-bools")
    text = response.content.decode("utf-8")
    assert "true" in text and "false" in text
    assert "True" not in text and "False" not in text


def test_json_integers_have_no_decimal_point() -> None:
    response = client.get("/json-numbers")
    text = response.content.decode("utf-8")
    parsed = json.loads(text)
    assert parsed["int"] == 42
    assert isinstance(parsed["int"], int)
    # Float must keep its decimal
    assert parsed["float"] == 3.14
    assert isinstance(parsed["float"], float)


def test_json_unicode_passes_through_as_utf8() -> None:
    response = client.get("/json-unicode")
    parsed = json.loads(response.content.decode("utf-8"))
    assert parsed["hello"] == "Привіт"
    assert parsed["emoji"] == "🎉"


def test_json_empty_dict_serializes_to_braces() -> None:
    response = client.get("/json-empty-dict")
    assert response.content == b"{}"


def test_json_empty_list_serializes_to_brackets() -> None:
    response = client.get("/json-empty-list")
    assert response.content == b"[]"


def test_json_top_level_array_starts_with_bracket() -> None:
    response = client.get("/json-list")
    assert response.content.startswith(b"[")
    assert response.content.endswith(b"]")
    assert response.json() == [1, 2, 3]


def test_json_negative_and_zero_numbers() -> None:
    response = client.get("/json-numbers")
    parsed = response.json()
    assert parsed["neg"] == -7
    assert parsed["zero"] == 0


# ---------------------------------------------------------------------------
# Status codes
# ---------------------------------------------------------------------------


def test_201_has_normal_body() -> None:
    """201 (unlike 204) DOES carry a body — must serialize the dict normally."""
    response = client.get("/created")
    assert response.status_code == 201
    assert response.json() == {"id": 42}
    assert response.headers["content-type"] == "application/json"


# ---------------------------------------------------------------------------
# HTML / Plain text
# ---------------------------------------------------------------------------


def test_html_response_content_type() -> None:
    response = client.get("/html")
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert response.text == "<h1>Hello</h1>"


def test_html_body_is_not_json_wrapped() -> None:
    """HTMLResponse body must be the raw HTML, not JSON-encoded."""
    response = client.get("/html")
    # Not wrapped in quotes (which would be JSON of a string)
    assert not response.content.startswith(b'"')
    assert response.content == b"<h1>Hello</h1>"


def test_plaintext_response_content_type() -> None:
    response = client.get("/plain")
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert response.text == "just text"


def test_plaintext_body_is_not_json_wrapped() -> None:
    response = client.get("/plain")
    assert not response.content.startswith(b'"')
    assert response.content == b"just text"


# ---------------------------------------------------------------------------
# Redirect
# ---------------------------------------------------------------------------


def test_redirect_default_307_with_location_header() -> None:
    response = client.get("/redirect", follow_redirects=False)
    assert response.status_code == 307
    # HTTP headers are case-insensitive on the way in
    assert response.headers["location"] == "/json-dict"


def test_redirect_explicit_308_status() -> None:
    response = client.get("/redirect-308", follow_redirects=False)
    assert response.status_code == 308
    assert response.headers["location"] == "/json-dict"


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------


def test_streaming_body_assembles_correctly() -> None:
    response = client.get("/stream")
    assert response.status_code == 200
    assert response.content == b"part-1 part-2 part-3"


def test_streaming_content_type_propagated() -> None:
    response = client.get("/stream")
    # StreamingResponse passes media_type through; charset added by Starlette for text/*
    assert response.headers["content-type"].startswith("text/plain")


# ---------------------------------------------------------------------------
# Headers
# ---------------------------------------------------------------------------


def test_response_headers_case_insensitive_lookup() -> None:
    response = client.get("/explicit-headers")
    # Both case variants must work on the client side
    assert response.headers["X-Trace"] == "abc-123"
    assert response.headers["x-trace"] == "abc-123"
    assert response.headers["X-TRACE"] == "abc-123"


def test_explicit_headers_propagate_to_response() -> None:
    response = client.get("/explicit-headers")
    assert response.headers["x-trace"] == "abc-123"
    assert response.headers["x-custom"] == "yes"


def test_content_length_matches_body_length() -> None:
    response = client.get("/json-dict")
    cl = response.headers.get("content-length")
    if cl is not None:
        assert int(cl) == len(response.content), (
            f"content-length={cl} but body is {len(response.content)} bytes"
        )


# ---------------------------------------------------------------------------
# Validation error format
# ---------------------------------------------------------------------------


def test_validation_error_response_is_json() -> None:
    """422 errors come back as JSON, not HTML or plain text."""
    bad_app = FastAPI()

    @bad_app.get("/v")
    def v(n: int) -> dict:
        return {"n": n}

    response = TestClient(bad_app).get("/v?n=abc")
    assert response.status_code == 422
    assert response.headers["content-type"] == "application/json"
    body = response.json()
    assert "detail" in body
    assert isinstance(body["detail"], list)


def test_404_uses_json_format() -> None:
    response = client.get("/no-such-thing")
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {"detail": "Not Found"}
