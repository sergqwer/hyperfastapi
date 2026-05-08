"""HTMLResponse — text/html, raw HTML body."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient

app = FastAPI()


@app.get("/page", response_class=HTMLResponse)
def page() -> str:
    return "<!doctype html><h1>Hello</h1>"


@app.get("/explicit")
def explicit() -> HTMLResponse:
    return HTMLResponse(content="<p>explicit</p>", status_code=201)


client = TestClient(app)


def test_html_content_type_with_charset() -> None:
    response = client.get("/page")
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/html; charset=utf-8"


def test_html_body_is_raw_not_json_wrapped() -> None:
    response = client.get("/page")
    assert response.text == "<!doctype html><h1>Hello</h1>"
    # Not JSON-quoted
    assert not response.content.startswith(b'"')


def test_html_explicit_response_status_code() -> None:
    response = client.get("/explicit")
    assert response.status_code == 201
    assert response.text == "<p>explicit</p>"
    assert response.headers["content-type"] == "text/html; charset=utf-8"


def test_html_unicode_body_passes_through() -> None:
    """Non-ASCII HTML must be encoded as UTF-8."""
    unicode_app = FastAPI()

    @unicode_app.get("/u", response_class=HTMLResponse)
    def u() -> str:
        return "<p>Привіт</p>"

    response = TestClient(unicode_app).get("/u")
    assert response.status_code == 200
    assert response.text == "<p>Привіт</p>"
