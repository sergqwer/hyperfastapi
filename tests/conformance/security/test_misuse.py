"""Adversarial security tests — what happens when the user sends WRONG things.

Conformance tests check the happy path. These tests check the 'sad path' that
defends FastAPI against client-side tampering: wrong auth headers, swapped
schemes, path traversal, content-type confusion, validation bypass attempts.

Critical for the Rust port: it's easy to accidentally accept malformed input
when reimplementing parsers. Each test here is a contract that the port must
preserve exactly to avoid regressions in security posture.
"""

from __future__ import annotations

import base64
import tempfile
from pathlib import Path
from typing import Annotated

import pytest
from fastapi import Depends, FastAPI, HTTPException, Security
from fastapi.security import (
    APIKeyCookie,
    APIKeyHeader,
    APIKeyQuery,
    HTTPBasic,
    HTTPBasicCredentials,
    HTTPBearer,
    OAuth2PasswordBearer,
    SecurityScopes,
)
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Common app with multiple security schemes for tampering tests
# ---------------------------------------------------------------------------

app = FastAPI()
bearer = HTTPBearer()
basic = HTTPBasic()
api_header = APIKeyHeader(name="X-API-Key")
api_query = APIKeyQuery(name="api_key")
api_cookie = APIKeyCookie(name="session")
oauth2 = OAuth2PasswordBearer(
    tokenUrl="/token",
    scopes={"read": "Read access", "write": "Write access"},
)


@app.get("/bearer")
def bearer_route(creds=Security(bearer)) -> dict:
    return {"creds": creds.credentials}


@app.get("/basic")
def basic_route(creds: HTTPBasicCredentials = Security(basic)) -> dict:
    return {"user": creds.username}


@app.get("/api-header")
def api_header_route(key: str = Security(api_header)) -> dict:
    return {"key": key}


@app.get("/api-query")
def api_query_route(key: str = Security(api_query)) -> dict:
    return {"key": key}


@app.get("/api-cookie")
def api_cookie_route(key: str = Security(api_cookie)) -> dict:
    return {"key": key}


def check_scope(scopes: SecurityScopes, token: str = Depends(oauth2)) -> dict:
    granted = set(token.split(","))
    for required in scopes.scopes:
        if required not in granted:
            raise HTTPException(status_code=403, detail=f"Missing scope: {required}")
    return {"scopes": list(granted)}


@app.get("/scoped-read", dependencies=[Security(check_scope, scopes=["read"])])
def scoped_read() -> dict:
    return {"ok": True}


@app.get("/scoped-write", dependencies=[Security(check_scope, scopes=["write"])])
def scoped_write() -> dict:
    return {"ok": True}


# Form-only and JSON-only routes for content-type confusion tests
class Item(BaseModel):
    name: str = Field(..., min_length=1)
    qty: int = Field(..., gt=0)


@app.post("/json-only")
def json_only(item: Item) -> dict:
    return item.model_dump()


from fastapi import Form  # noqa: E402


@app.post("/form-only")
def form_only(name: Annotated[str, Form()], qty: Annotated[int, Form()]) -> dict:
    return {"name": name, "qty": qty}


client = TestClient(app)


# ---------------------------------------------------------------------------
# Authorization header tampering
# ---------------------------------------------------------------------------


def test_bearer_empty_credentials_rejected() -> None:
    """`Authorization: Bearer ` (trailing space, empty token) → 403."""
    response = client.get("/bearer", headers={"Authorization": "Bearer "})
    assert response.status_code == 403


def test_bearer_no_scheme_just_token() -> None:
    """`Authorization: just-a-token` (no scheme prefix) → 403."""
    response = client.get("/bearer", headers={"Authorization": "just-a-token"})
    assert response.status_code == 403


def test_bearer_basic_scheme_rejected() -> None:
    """Bearer endpoint must reject Basic auth credentials."""
    encoded = base64.b64encode(b"user:pass").decode()
    response = client.get("/bearer", headers={"Authorization": f"Basic {encoded}"})
    assert response.status_code == 403


def test_bearer_just_the_word_bearer() -> None:
    """`Authorization: Bearer` (no space, no token) → 403."""
    response = client.get("/bearer", headers={"Authorization": "Bearer"})
    assert response.status_code == 403


def test_basic_with_malformed_base64_rejected() -> None:
    """Basic with non-base64 garbage in the credentials part → 401."""
    response = client.get("/basic", headers={"Authorization": "Basic !!!notbase64!!!"})
    assert response.status_code == 401


def test_basic_with_bearer_scheme_rejected() -> None:
    """Basic endpoint must reject Bearer-formatted credentials."""
    response = client.get("/basic", headers={"Authorization": "Bearer abc123"})
    assert response.status_code == 401


def test_basic_with_no_colon_in_decoded_credentials() -> None:
    """`Basic <base64-without-colon>` → 401 (Basic auth requires user:pass form)."""
    encoded = base64.b64encode(b"no-colon-here").decode()
    response = client.get("/basic", headers={"Authorization": f"Basic {encoded}"})
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# APIKey transport-confusion tests
# ---------------------------------------------------------------------------


def test_apikey_header_scheme_rejects_key_in_query() -> None:
    """APIKeyHeader scheme — sending the key as ?api_key=... must NOT authenticate."""
    response = client.get("/api-header?X-API-Key=abc&api_key=abc")
    assert response.status_code == 403


def test_apikey_query_scheme_rejects_key_in_header() -> None:
    """APIKeyQuery scheme — sending the key in X-API-Key header must NOT authenticate."""
    response = client.get("/api-query", headers={"X-API-Key": "abc", "api_key": "abc"})
    assert response.status_code == 403


def test_apikey_cookie_scheme_rejects_key_in_header() -> None:
    response = client.get("/api-cookie", headers={"X-API-Key": "abc", "session": "abc"})
    assert response.status_code == 403


def test_apikey_with_special_chars_passes_through() -> None:
    """API keys are opaque strings; special chars must NOT be rejected by FastAPI."""
    weird = "abc!@#$%^&*()_+{}[]"
    response = client.get("/api-header", headers={"X-API-Key": weird})
    assert response.status_code == 200
    assert response.json() == {"key": weird}


# ---------------------------------------------------------------------------
# StaticFiles path traversal
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def static_app() -> FastAPI:
    tmp = Path(tempfile.mkdtemp(prefix="misuse-static-"))
    (tmp / "public.txt").write_bytes(b"public content")
    # A file OUTSIDE the served directory — must NEVER be reachable.
    parent = tmp.parent
    secret = parent / "secret.txt"
    secret.write_bytes(b"SECRET")
    a = FastAPI()
    a.mount("/static", StaticFiles(directory=str(tmp)), name="static")
    return a


def test_traversal_dotdot_returns_404(static_app: FastAPI) -> None:
    """`/static/../secret.txt` must NOT escape the served directory."""
    response = TestClient(static_app).get("/static/../secret.txt")
    assert response.status_code == 404


def test_traversal_double_dotdot_returns_404(static_app: FastAPI) -> None:
    response = TestClient(static_app).get("/static/../../etc/passwd")
    assert response.status_code == 404


def test_traversal_url_encoded_dotdot_returns_404(static_app: FastAPI) -> None:
    """URL-encoded `..%2Fsecret` must also be blocked."""
    response = TestClient(static_app).get("/static/..%2Fsecret.txt")
    # Could be 404 or 400 — either way, MUST not be 200
    assert response.status_code != 200


def test_traversal_legitimate_request_still_works(static_app: FastAPI) -> None:
    """Sanity: clean path still serves the file."""
    response = TestClient(static_app).get("/static/public.txt")
    assert response.status_code == 200
    assert response.content == b"public content"


# ---------------------------------------------------------------------------
# Content-Type / body-shape confusion
# ---------------------------------------------------------------------------


def test_form_data_to_json_route_returns_422() -> None:
    """A JSON-only route must reject form-encoded body."""
    response = client.post("/json-only", data={"name": "x", "qty": "1"})
    assert response.status_code == 422


def test_json_to_form_route_returns_422() -> None:
    """A form-only route must reject JSON body."""
    response = client.post("/form-only", json={"name": "x", "qty": 1})
    assert response.status_code == 422


def test_text_plain_to_json_route_returns_422() -> None:
    """Wrong Content-Type with JSON-shaped bytes must still be rejected."""
    response = client.post(
        "/json-only",
        content=b'{"name": "x", "qty": 1}',
        headers={"Content-Type": "text/plain"},
    )
    assert response.status_code == 422


def test_empty_body_to_required_json_route() -> None:
    response = client.post("/json-only")
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Pydantic validation evasion
# ---------------------------------------------------------------------------


def test_array_where_object_expected_returns_422() -> None:
    response = client.post("/json-only", json=[{"name": "x", "qty": 1}])
    assert response.status_code == 422


def test_object_where_int_field_expected_returns_422() -> None:
    response = client.post("/json-only", json={"name": "x", "qty": {"$ne": 0}})
    assert response.status_code == 422
    err = response.json()["detail"][0]
    assert err["loc"] == ["body", "qty"]


def test_string_null_is_not_python_none() -> None:
    """`"null"` (the string) must not be accepted where None is required —
    Pydantic should reject it as wrong type for int.
    """
    response = client.post("/json-only", json={"name": "x", "qty": "null"})
    assert response.status_code == 422


def test_negative_value_violates_gt_constraint() -> None:
    response = client.post("/json-only", json={"name": "x", "qty": -1})
    assert response.status_code == 422
    err = response.json()["detail"][0]
    assert err["loc"] == ["body", "qty"]


def test_empty_string_violates_min_length() -> None:
    response = client.post("/json-only", json={"name": "", "qty": 1})
    assert response.status_code == 422
    err = response.json()["detail"][0]
    assert err["loc"] == ["body", "name"]


# ---------------------------------------------------------------------------
# Security scopes bypass
# ---------------------------------------------------------------------------


def test_scoped_read_with_no_scopes_in_token_rejected() -> None:
    """Empty Bearer token passes the scheme check (OAuth2PasswordBearer validates
    only scheme, not token shape), so the request reaches our scope-check dep,
    which sees an empty scope set and rejects with 403.
    """
    response = client.get("/scoped-read", headers={"Authorization": "Bearer "})
    assert response.status_code == 403


def test_scoped_read_with_wrong_scope_returns_403() -> None:
    """Token has 'write' but route requires 'read' — denied."""
    response = client.get("/scoped-read", headers={"Authorization": "Bearer write"})
    assert response.status_code == 403


def test_scoped_write_with_only_read_returns_403() -> None:
    response = client.get("/scoped-write", headers={"Authorization": "Bearer read"})
    assert response.status_code == 403


def test_scoped_with_typo_in_scope_returns_403() -> None:
    """User-side 'reed' (typo of 'read') must not match — strict comparison."""
    response = client.get("/scoped-read", headers={"Authorization": "Bearer reed"})
    assert response.status_code == 403


def test_scoped_extra_scopes_still_pass() -> None:
    """Token with extra scopes beyond what's required — should still pass."""
    response = client.get(
        "/scoped-read", headers={"Authorization": "Bearer read,write,admin"}
    )
    assert response.status_code == 200
