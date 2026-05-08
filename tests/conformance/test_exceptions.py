"""HTTPException, custom exception handlers."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.testclient import TestClient

app = FastAPI()


@app.get("/raise-404")
def raise_404() -> dict:
    raise HTTPException(status_code=404, detail="Item not found")


@app.get("/raise-with-headers")
def raise_with_headers() -> dict:
    raise HTTPException(
        status_code=400,
        detail="bad",
        headers={"X-Error-Code": "E_BAD_REQUEST"},
    )


@app.get("/raise-detail-dict")
def raise_detail_dict() -> dict:
    raise HTTPException(status_code=422, detail={"error": "validation", "field": "name"})


# Custom exception handler for a user-defined exception
class MyError(Exception):
    def __init__(self, code: str):
        self.code = code


app2 = FastAPI()


@app2.exception_handler(MyError)
async def my_error_handler(request: Request, exc: MyError) -> JSONResponse:
    return JSONResponse(status_code=418, content={"error_code": exc.code})


@app2.get("/raise-custom")
def raise_custom() -> dict:
    raise MyError("E_TEAPOT")


# Override the default HTTPException handler
app3 = FastAPI()


@app3.exception_handler(HTTPException)
async def http_exc_text_handler(request: Request, exc: HTTPException) -> PlainTextResponse:
    return PlainTextResponse(content=f"{exc.status_code}: {exc.detail}", status_code=exc.status_code)


@app3.get("/x")
def x() -> dict:
    raise HTTPException(status_code=403, detail="forbidden")


client = TestClient(app)
client2 = TestClient(app2)
client3 = TestClient(app3)


def test_httpexception_status_and_detail() -> None:
    response = client.get("/raise-404")
    assert response.status_code == 404
    assert response.json() == {"detail": "Item not found"}


def test_httpexception_content_type_is_json() -> None:
    response = client.get("/raise-404")
    assert response.headers["content-type"] == "application/json"


def test_httpexception_custom_headers_propagated() -> None:
    response = client.get("/raise-with-headers")
    assert response.status_code == 400
    assert response.headers["x-error-code"] == "E_BAD_REQUEST"


def test_httpexception_dict_detail() -> None:
    """detail can be any JSON-serializable type, including dicts."""
    response = client.get("/raise-detail-dict")
    assert response.status_code == 422
    assert response.json() == {"detail": {"error": "validation", "field": "name"}}


def test_custom_exception_handler_invoked_for_user_exception() -> None:
    response = client2.get("/raise-custom")
    assert response.status_code == 418
    assert response.json() == {"error_code": "E_TEAPOT"}


def test_overriding_httpexception_handler_changes_response_format() -> None:
    """An app can replace the default {'detail': ...} format with anything."""
    response = client3.get("/x")
    assert response.status_code == 403
    assert response.headers["content-type"].startswith("text/plain")
    assert response.text == "403: forbidden"


def test_404_for_unknown_route_default_format() -> None:
    """Unmatched route — default 404 format is {'detail': 'Not Found'}."""
    response = client.get("/no-such-path")
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


def test_405_for_method_not_allowed() -> None:
    """A method-not-allowed response uses the same {'detail': ...} format."""
    response = client.post("/raise-404")
    assert response.status_code == 405
    assert response.json() == {"detail": "Method Not Allowed"}
