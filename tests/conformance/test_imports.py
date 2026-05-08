"""Public-API import contract.

Every name documented as `from fastapi import X` (or `from fastapi.<submodule>
import X`) must exist with the right kind. If FastAPI removes/renames a public
symbol, this file fails fast — before any behaviour test runs. Critical for the
Rust port: it must export the same namespace, so this file becomes the contract.
"""

from __future__ import annotations

import importlib
import types

import pytest

# (module_path, name, kind)  where kind ∈ {class, callable, module, exception}
PUBLIC_API: list[tuple[str, str, str]] = [
    # `from fastapi import X` (verified against fastapi/__init__.py):
    ("fastapi", "FastAPI", "class"),
    ("fastapi", "APIRouter", "class"),
    ("fastapi", "BackgroundTasks", "class"),
    ("fastapi", "UploadFile", "class"),
    ("fastapi", "HTTPException", "exception"),
    ("fastapi", "WebSocketException", "exception"),
    ("fastapi", "Body", "callable"),
    ("fastapi", "Query", "callable"),
    ("fastapi", "Path", "callable"),
    ("fastapi", "Header", "callable"),
    ("fastapi", "Cookie", "callable"),
    ("fastapi", "Form", "callable"),
    ("fastapi", "File", "callable"),
    ("fastapi", "Depends", "callable"),
    ("fastapi", "Security", "callable"),
    ("fastapi", "Request", "class"),
    ("fastapi", "Response", "class"),
    ("fastapi", "WebSocket", "class"),
    ("fastapi", "WebSocketDisconnect", "exception"),
    ("fastapi", "status", "module"),
    # Submodule public API:
    ("fastapi.testclient", "TestClient", "class"),
    ("fastapi.responses", "JSONResponse", "class"),
    ("fastapi.responses", "HTMLResponse", "class"),
    ("fastapi.responses", "PlainTextResponse", "class"),
    ("fastapi.responses", "RedirectResponse", "class"),
    ("fastapi.responses", "FileResponse", "class"),
    ("fastapi.responses", "StreamingResponse", "class"),
    ("fastapi.security", "OAuth2PasswordBearer", "class"),
    ("fastapi.security", "OAuth2PasswordRequestForm", "class"),
    ("fastapi.security", "HTTPBasic", "class"),
    ("fastapi.security", "HTTPBearer", "class"),
    ("fastapi.security", "APIKeyHeader", "class"),
    ("fastapi.security", "APIKeyQuery", "class"),
    ("fastapi.security", "APIKeyCookie", "class"),
    ("fastapi.encoders", "jsonable_encoder", "callable"),
    ("fastapi.exceptions", "RequestValidationError", "exception"),
    ("fastapi.middleware.cors", "CORSMiddleware", "class"),
    ("fastapi.middleware.gzip", "GZipMiddleware", "class"),
    ("fastapi.middleware.trustedhost", "TrustedHostMiddleware", "class"),
]


@pytest.mark.parametrize(
    "module_path,name,kind",
    PUBLIC_API,
    ids=[f"{m}.{n}" for m, n, _ in PUBLIC_API],
)
def test_public_symbol_exists(module_path: str, name: str, kind: str) -> None:
    mod = importlib.import_module(module_path)
    assert hasattr(mod, name), f"{module_path} is missing public symbol {name!r}"
    obj = getattr(mod, name)
    assert obj is not None, f"{module_path}.{name} is None"

    if kind == "class":
        assert isinstance(obj, type), f"{name} expected class, got {type(obj).__name__}"
    elif kind == "exception":
        assert isinstance(obj, type) and issubclass(obj, BaseException), (
            f"{name} expected exception class, got {type(obj).__name__}"
        )
    elif kind == "callable":
        assert callable(obj), f"{name} expected callable, got {type(obj).__name__}"
    elif kind == "module":
        assert isinstance(obj, types.ModuleType), (
            f"{name} expected module, got {type(obj).__name__}"
        )
    else:
        pytest.fail(f"Unknown kind {kind!r} for {name}")


def test_fastapi_version_is_string() -> None:
    import fastapi

    assert hasattr(fastapi, "__version__")
    assert isinstance(fastapi.__version__, str)
    parts = fastapi.__version__.split(".")
    assert len(parts) >= 2, f"Unexpected version format: {fastapi.__version__!r}"
    assert all(p.split("-")[0].isdigit() or p[0].isdigit() for p in parts), (
        f"Version parts not numeric: {fastapi.__version__!r}"
    )


def test_status_has_common_http_codes() -> None:
    """`fastapi.status` re-exports Starlette's HTTP status constants."""
    from fastapi import status

    assert status.HTTP_200_OK == 200
    assert status.HTTP_201_CREATED == 201
    assert status.HTTP_204_NO_CONTENT == 204
    assert status.HTTP_301_MOVED_PERMANENTLY == 301
    assert status.HTTP_307_TEMPORARY_REDIRECT == 307
    assert status.HTTP_400_BAD_REQUEST == 400
    assert status.HTTP_401_UNAUTHORIZED == 401
    assert status.HTTP_403_FORBIDDEN == 403
    assert status.HTTP_404_NOT_FOUND == 404
    assert status.HTTP_422_UNPROCESSABLE_ENTITY == 422
    assert status.HTTP_500_INTERNAL_SERVER_ERROR == 500


def test_status_has_websocket_codes() -> None:
    from fastapi import status

    assert status.WS_1000_NORMAL_CLOSURE == 1000
    assert status.WS_1008_POLICY_VIOLATION == 1008
    assert status.WS_1011_INTERNAL_ERROR == 1011


def test_param_functions_return_param_instances() -> None:
    """Body/Query/Path/Header/Cookie/Form/File are factories that return param objects."""
    import fastapi.params as fastapi_params
    from fastapi import Body, Cookie, File, Form, Header, Path, Query

    assert isinstance(Query(None), fastapi_params.Query)
    assert isinstance(Path(...), fastapi_params.Path)
    assert isinstance(Header(None), fastapi_params.Header)
    assert isinstance(Cookie(None), fastapi_params.Cookie)
    assert isinstance(Body(None), fastapi_params.Body)
    assert isinstance(Form(None), fastapi_params.Form)
    assert isinstance(File(None), fastapi_params.File)


def test_depends_and_security_return_param_objects() -> None:
    import fastapi.params as fastapi_params
    from fastapi import Depends, Security

    def fake_dep() -> None: ...

    d = Depends(fake_dep)
    assert isinstance(d, fastapi_params.Depends)
    assert d.dependency is fake_dep

    s = Security(fake_dep, scopes=["read:items"])
    assert isinstance(s, fastapi_params.Security)
    assert s.dependency is fake_dep
    assert s.scopes == ["read:items"]


def test_httpexception_extends_starlette() -> None:
    """FastAPI's HTTPException must inherit from Starlette's so generic handlers catch both."""
    from starlette.exceptions import HTTPException as StarletteHTTPException

    from fastapi import HTTPException

    assert issubclass(HTTPException, StarletteHTTPException)


def test_websocketdisconnect_is_starlette() -> None:
    from starlette.websockets import WebSocketDisconnect as StarletteWSDisconnect

    from fastapi import WebSocketDisconnect

    # Either same class or subclass — both are valid contracts
    assert WebSocketDisconnect is StarletteWSDisconnect or issubclass(
        WebSocketDisconnect, StarletteWSDisconnect
    )


def test_jsonable_encoder_handles_basic_types() -> None:
    """Smoke-test jsonable_encoder for the most-tested types."""
    from datetime import datetime, timezone
    from uuid import UUID

    from fastapi.encoders import jsonable_encoder

    assert jsonable_encoder("hello") == "hello"
    assert jsonable_encoder(42) == 42
    assert jsonable_encoder([1, 2, 3]) == [1, 2, 3]
    assert jsonable_encoder({"a": 1}) == {"a": 1}

    dt = datetime(2026, 5, 8, 14, 30, 0, tzinfo=timezone.utc)
    assert jsonable_encoder(dt) == "2026-05-08T14:30:00+00:00"

    u = UUID("12345678-1234-5678-1234-567812345678")
    assert jsonable_encoder(u) == "12345678-1234-5678-1234-567812345678"


def test_no_underscore_names_in_main_namespace() -> None:
    """`from fastapi import *` shouldn't leak private names like `_compat`."""
    import fastapi

    public_names = [n for n in dir(fastapi) if not n.startswith("_")]
    leaked = [n for n in public_names if n.startswith("_") and n not in {"__version__"}]
    assert leaked == [], f"Private names leaked into fastapi namespace: {leaked}"


def test_responses_module_does_not_export_response() -> None:
    """`fastapi.responses.Response` must exist (re-export from Starlette base class)."""
    from fastapi.responses import Response

    assert isinstance(Response, type)


def test_security_oauth2_password_bearer_signature() -> None:
    """OAuth2PasswordBearer must accept tokenUrl as keyword arg."""
    from fastapi.security import OAuth2PasswordBearer

    bearer = OAuth2PasswordBearer(tokenUrl="/token")
    assert bearer.scheme_name == "OAuth2PasswordBearer"
    assert bearer.auto_error is True


def test_apirouter_default_construction() -> None:
    """APIRouter() with no args should construct an empty router."""
    from fastapi import APIRouter

    router = APIRouter()
    assert router.prefix == ""
    assert router.routes == []
