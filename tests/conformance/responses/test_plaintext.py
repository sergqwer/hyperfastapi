"""PlainTextResponse — text/plain, raw string body."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient

app = FastAPI()


@app.get("/text", response_class=PlainTextResponse)
def text() -> str:
    return "hello world"


@app.get("/explicit")
def explicit() -> PlainTextResponse:
    return PlainTextResponse(content="ok", status_code=202)


client = TestClient(app)


def test_plaintext_content_type_with_charset() -> None:
    response = client.get("/text")
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/plain; charset=utf-8"


def test_plaintext_body_is_raw() -> None:
    response = client.get("/text")
    assert response.text == "hello world"
    assert response.content == b"hello world"


def test_plaintext_explicit_status() -> None:
    response = client.get("/explicit")
    assert response.status_code == 202
    assert response.text == "ok"


def test_plaintext_unicode_passthrough() -> None:
    unicode_app = FastAPI()

    @unicode_app.get("/u", response_class=PlainTextResponse)
    def u() -> str:
        return "Привіт 🌍"

    response = TestClient(unicode_app).get("/u")
    assert response.status_code == 200
    assert response.text == "Привіт 🌍"


def test_plaintext_no_quotes_added() -> None:
    """A returned string in text/plain must NOT be wrapped in JSON quotes."""
    response = client.get("/text")
    assert not response.content.startswith(b'"')
