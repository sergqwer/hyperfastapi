"""ASGI bridge — Python subclass of the Rust ``_core.FastAPI`` PyClass.

Phase F adds:
    - User exception handlers (HTTPException, RequestValidationError, custom
      exception types) registered via ``@app.exception_handler(...)`` or
      ``app.add_exception_handler(...)``.
    - ASGI middleware stack: ``app.add_middleware(MidClass, **opts)`` plus the
      ``@app.middleware("http")`` function-decorator form. The chain is built
      once on first request and cached.
    - BackgroundTasks: a handler can declare ``bg: BackgroundTasks`` and call
      ``bg.add_task(fn, ...)``; tasks run after the response body is sent.
    - Lifespan: ``FastAPI(lifespan=asynccontextmanager_fn)`` runs startup/
      teardown around the request loop. Deprecated ``@app.on_event("startup")``
      / ``"shutdown"`` decorators are also honoured (collected into a synthetic
      lifespan).

The Rust side still owns route matching + parameter extraction + Pydantic
validation; this module owns the ASGI lifecycle and error-to-response mapping.
"""

from __future__ import annotations

import asyncio
import inspect
import traceback
from contextlib import asynccontextmanager
from typing import Any, Awaitable, Callable, Iterable

from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware import Middleware
from starlette.middleware.errors import ServerErrorMiddleware
from starlette.middleware.exceptions import ExceptionMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.types import Receive, Scope, Send

from ._core import FastAPI as _RustFastAPI
from . import _bg as _bg_state
from .exceptions import HTTPException, RequestValidationError


def _default_http_exception_handler(_request: Request, exc: HTTPException) -> Response:
    """FastAPI's default HTTPException renderer — `{"detail": ...}` JSON, with
    the exception's headers (e.g. WWW-Authenticate) merged in.
    """
    headers = getattr(exc, "headers", None)
    return JSONResponse(
        {"detail": exc.detail},
        status_code=exc.status_code,
        headers=headers,
    )


def _default_request_validation_error_handler(
    _request: Request,
    exc: RequestValidationError,
) -> Response:
    return JSONResponse({"detail": exc.errors()}, status_code=422)


class FastAPI(_RustFastAPI):
    """Sibling-package replacement for ``fastapi.FastAPI``.

    PyO3-backed PyClasses don't support cooperative ``super().__init__`` with
    arbitrary kwargs from a Python subclass — the constructor protocol stops
    at the Rust ``#[new]`` and ``object.__init__`` rejects extra args. We
    instead extract the ``lifespan`` kwarg via ``__new__`` (which is the
    layer where we still control argument routing) and lazy-initialise the
    rest of our Python state on first access.
    """

    def __new__(cls, *args: Any, **kwargs: Any) -> "FastAPI":
        lifespan = kwargs.pop("lifespan", None)
        instance = _RustFastAPI.__new__(cls, *args, **kwargs)
        instance._lifespan_user = lifespan  # type: ignore[attr-defined]
        return instance

    def _ensure_state(self) -> None:
        if not hasattr(self, "_exception_handlers"):
            self._exception_handlers = {
                HTTPException: _default_http_exception_handler,
                StarletteHTTPException: _default_http_exception_handler,
                RequestValidationError: _default_request_validation_error_handler,
            }
            self._user_middleware = []
            self._on_startup = []
            self._on_shutdown = []
            if not hasattr(self, "_lifespan_user"):
                self._lifespan_user = None
            self._middleware_stack = None
            self._mounts: list[tuple[str, Any, str | None]] = []  # (path, app, name)

    # ------------------------------------------------------------------
    # Mount sub-ASGI app at a path prefix (StaticFiles, sub-FastAPI, etc.)
    # ------------------------------------------------------------------

    def mount(self, path: str, app: Any, name: str | None = None) -> None:
        """Mount a sub-ASGI app at ``path``. Requests under ``path/...`` are
        forwarded to ``app`` with the prefix stripped from ``scope['path']``.
        """
        self._ensure_state()
        if not path.startswith("/"):
            path = "/" + path
        # Drop trailing slash for matching consistency.
        if path.endswith("/") and len(path) > 1:
            path = path[:-1]
        self._mounts.append((path, app, name))

    # ------------------------------------------------------------------
    # Registration API
    # ------------------------------------------------------------------

    def exception_handler(
        self, exc_class_or_status_code: type | int
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            self.add_exception_handler(exc_class_or_status_code, func)
            return func

        return decorator

    def add_exception_handler(
        self,
        exc_class_or_status_code: type | int,
        handler: Callable[..., Any],
    ) -> None:
        self._ensure_state()
        # We only support exception-class keys; status-code keys are a Starlette
        # ergonomic that's rarely used and adds complexity. Phase J revisit.
        if isinstance(exc_class_or_status_code, int):
            return
        self._exception_handlers[exc_class_or_status_code] = handler
        # Reset middleware stack so the next request rebuilds with the new map.
        self._middleware_stack = None

    def add_middleware(self, middleware_class: type, **options: Any) -> None:
        self._ensure_state()
        self._user_middleware.insert(0, Middleware(middleware_class, **options))
        self._middleware_stack = None

    def middleware(self, middleware_type: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """`@app.middleware("http")` — wraps a function as a BaseHTTPMiddleware."""
        if middleware_type != "http":
            raise NotImplementedError(f"middleware type {middleware_type!r} not supported")
        from starlette.middleware.base import BaseHTTPMiddleware

        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            class _UserMiddleware(BaseHTTPMiddleware):
                async def dispatch(self, request, call_next):  # type: ignore[override]
                    return await func(request, call_next)

            self.add_middleware(_UserMiddleware)
            return func

        return decorator

    def on_event(self, event_type: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Deprecated: prefer ``lifespan=...``. Still wired so existing code runs."""
        if event_type not in ("startup", "shutdown"):
            raise ValueError(f"unknown on_event type: {event_type!r}")
        self._ensure_state()

        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            if event_type == "startup":
                self._on_startup.append(func)
            else:
                self._on_shutdown.append(func)
            return func

        return decorator

    # ------------------------------------------------------------------
    # ASGI plumbing
    # ------------------------------------------------------------------

    def _build_middleware_stack(self) -> Callable[[Scope, Receive, Send], Awaitable[None]]:
        """Compose: ServerErrorMiddleware → user middleware → ExceptionMiddleware → core_app.

        ServerErrorMiddleware catches uncaught Exception and returns 500.
        ExceptionMiddleware dispatches HTTPException + custom registered handlers.
        """
        # Innermost: the core dispatcher that calls Rust `_dispatch`.
        app: Callable[..., Awaitable[None]] = self._core_http_app

        # Status-code handlers are not used; class handlers map ExceptionMiddleware.
        exc_class_handlers: dict[type, Callable[..., Any]] = {}
        for exc_cls, handler in self._exception_handlers.items():
            exc_class_handlers[exc_cls] = handler

        # ExceptionMiddleware — converts HTTPException + user-registered classes
        # into responses via the registered handlers.
        app = ExceptionMiddleware(
            app,
            handlers=exc_class_handlers,  # type: ignore[arg-type]
            debug=False,
        )

        # User middleware: outer wraps inner. We stored newest-first, so iterate
        # in reverse to apply oldest first (innermost), letting the newest end
        # up outermost — matches Starlette's add_middleware semantics.
        for m in reversed(self._user_middleware):
            app = m.cls(app, *m.args, **m.kwargs)

        # ServerErrorMiddleware: outermost; catches anything not caught downstream.
        app = ServerErrorMiddleware(app, debug=False)
        return app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        self._ensure_state()
        scope_type = scope.get("type")

        if scope_type == "lifespan":
            await self._lifespan(scope, receive, send)
            return

        if self._middleware_stack is None:
            self._middleware_stack = self._build_middleware_stack()

        # The middleware stack handles both http and websocket scopes uniformly.
        await self._middleware_stack(scope, receive, send)

    # ------------------------------------------------------------------
    # Lifespan
    # ------------------------------------------------------------------

    async def _lifespan(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Drive lifespan events. Pattern mirrors Starlette's own:
        we call ``async with`` over the user lifespan (or a synthetic one
        composed from ``on_event`` handlers), sending startup.complete after
        entering and shutdown.complete after the natural exit. Failures bubble
        up as ``lifespan.{startup,shutdown}.failed`` and a re-raise so
        TestClient surfaces them as proper Python exceptions.
        """
        if self._lifespan_user is not None:
            lifespan_cm = self._lifespan_user(self)
        else:

            @asynccontextmanager
            async def _on_event_lifespan(_app: FastAPI):
                for fn in self._on_startup:
                    if inspect.iscoroutinefunction(fn):
                        await fn()
                    else:
                        fn()
                yield
                for fn in self._on_shutdown:
                    if inspect.iscoroutinefunction(fn):
                        await fn()
                    else:
                        fn()

            lifespan_cm = _on_event_lifespan(self)

        await receive()  # lifespan.startup
        started = False
        try:
            async with lifespan_cm:
                await send({"type": "lifespan.startup.complete"})
                started = True
                await receive()  # lifespan.shutdown
        except BaseException:
            tb = traceback.format_exc()
            if started:
                await send({"type": "lifespan.shutdown.failed", "message": tb})
            else:
                await send({"type": "lifespan.startup.failed", "message": tb})
            raise
        await send({"type": "lifespan.shutdown.complete"})

    # ------------------------------------------------------------------
    # Innermost ASGI app — calls Rust `_dispatch`
    # ------------------------------------------------------------------

    async def _core_http_app(self, scope: Scope, receive: Receive, send: Send) -> None:
        scope_type = scope.get("type")
        if scope_type == "websocket":
            await self._handle_websocket(scope, receive, send)
            return
        if scope_type != "http":
            return

        method: str = scope["method"]
        path: str = scope["path"]

        # Phase I: forward to a mounted sub-ASGI app if the path prefix matches.
        # Iterate in registration order; first prefix wins. Strip prefix from
        # scope["path"] and put the rest in scope["root_path"] so apps that
        # build their own URLs (StaticFiles) see the right values.
        if getattr(self, "_mounts", None):
            for prefix, sub_app, _name in self._mounts:
                if path == prefix or path.startswith(prefix + "/"):
                    sub_path = path[len(prefix):] or "/"
                    sub_scope = dict(scope)
                    sub_scope["path"] = sub_path
                    sub_scope["root_path"] = scope.get("root_path", "") + prefix
                    await sub_app(sub_scope, receive, send)
                    return

        # Phase H: intercept /openapi.json before handing to Rust so the
        # Python-built schema (with parameters/requestBody/components) wins
        # over the Rust skeleton.
        if method in ("GET", "HEAD"):
            openapi_url = self.openapi_url
            docs_url = self.docs_url
            redoc_url = self.redoc_url
            # When openapi_url is None, the schema endpoint and the docs
            # pages that depend on it must all 404 — they have no source
            # of truth to render.
            if openapi_url is None and path in (
                "/openapi.json",
                docs_url or "/docs",
                redoc_url or "/redoc",
            ):
                await send({
                    "type": "http.response.start",
                    "status": 404,
                    "headers": [(b"content-type", b"application/json")],
                })
                await send({"type": "http.response.body", "body": b'{"detail":"Not Found"}', "more_body": False})
                return
            if openapi_url and path == openapi_url:
                from . import _openapi as _oa
                schema = _oa.build_openapi_schema(self)
                import json as _json
                body_bytes = _json.dumps(schema).encode("utf-8")
                if method == "HEAD":
                    body_bytes = b""
                await send({
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"application/json")],
                })
                await send({"type": "http.response.body", "body": body_bytes, "more_body": False})
                return
            # Phase H: also serve /docs/oauth2-redirect (Swagger UI helper).
            if docs_url and path == f"{docs_url}/oauth2-redirect":
                from .docs_html import OAUTH2_REDIRECT_HTML
                body_bytes = OAUTH2_REDIRECT_HTML.encode("utf-8") if method == "GET" else b""
                await send({
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"text/html; charset=utf-8")],
                })
                await send({"type": "http.response.body", "body": body_bytes, "more_body": False})
                return

        query_bytes = scope.get("query_string") or b""
        query_string = (
            query_bytes.decode("latin-1")
            if isinstance(query_bytes, (bytes, bytearray))
            else str(query_bytes)
        )
        raw_headers: Iterable[tuple[bytes, bytes]] = scope.get("headers") or []
        headers_list: list[tuple[str, str]] = [
            (k.decode("latin-1"), v.decode("latin-1")) for k, v in raw_headers
        ]
        body = bytearray()
        while True:
            msg = await receive()
            if msg.get("type") == "http.request":
                body.extend(msg.get("body") or b"")
                if not msg.get("more_body", False):
                    break
            elif msg.get("type") == "http.disconnect":
                return

        # Synchronous dispatch into Rust. HTTPException raised by handlers
        # surfaces as a PyErr; ExceptionMiddleware (one layer up) catches and
        # routes to the registered handler. We construct a Starlette Request
        # eagerly so handlers declaring ``request: Request`` get a real one.
        request_obj = Request(scope, receive=receive)
        status, headers, response_body, background, raw_resp = await self._dispatch_async(
            method, path, query_string, headers_list, bytes(body), request_obj
        )

        # Phase F+: handler returned a StreamingResponse/FileResponse. Drive
        # it natively so async-only bodies can stream.
        if raw_resp is not None:
            if background is not None and getattr(raw_resp, "background", None) is None:
                raw_resp.background = background
            await raw_resp(scope, receive, send)
            return

        headers_bytes: list[tuple[bytes, bytes]] = [
            (k.encode("latin-1"), v.encode("latin-1")) for k, v in headers
        ]
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": headers_bytes,
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": bytes(response_body),
                "more_body": False,
            }
        )

        # BackgroundTasks: run after body sent. FIFO order, sync+async both ok.
        if background is not None:
            await background()

    async def _handle_websocket(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Phase G: route websocket connections through Starlette's WebSocket
        wrapper. The Rust side stores websocket routes keyed by path; we look
        up the handler and invoke it with a Starlette WebSocket so async user
        code can do ``await ws.accept(); await ws.send_text(...)``.
        """
        from starlette.websockets import WebSocket as StarletteWebSocket

        path = scope.get("path", "")
        handler = self._lookup_websocket(path)
        if handler is None:
            await send({"type": "websocket.close", "code": 1000})
            return
        ws = StarletteWebSocket(scope, receive=receive, send=send)
        try:
            if inspect.iscoroutinefunction(handler):
                await handler(ws)
            else:
                handler(ws)  # sync handler — rare; let user own the protocol
        except Exception:
            # If handler raises, ensure the connection is closed so TestClient
            # doesn't hang waiting for a frame.
            try:
                await ws.close(code=1011)
            except Exception:
                pass
            raise

    async def _dispatch_async(
        self,
        method: str,
        path: str,
        query_string: str,
        headers_list: list[tuple[str, str]],
        body: bytes,
        request: Any = None,
    ) -> tuple[int, list[tuple[str, str]], bytes, Any, Any]:
        """Wrap the sync Rust dispatcher. ``request`` is a Starlette Request
        constructed by the ASGI layer; it's stashed so handler params declared
        as ``request: Request`` can receive it.

        Returns ``(status, headers, body, background, raw_response)``. When
        ``raw_response`` is not None, the ASGI layer drives it directly; when
        it is None, the ``status/headers/body`` triple is sent as-is.
        """
        _bg_state._current_tasks = None
        _bg_state._current_request = request
        _bg_state._current_raw_response = None
        # Phase L: every request gets a fresh yield-dep generator stack.
        # Rust dispatch drains it after the handler runs (before this method
        # returns). Anything still here at finally time means dispatch
        # bailed out before drain — close them out so resources don't leak.
        _bg_state._current_yield_gens = []
        try:
            status, headers, response_body = self._dispatch(
                method, path, query_string, headers_list, body
            )
            tasks = _bg_state._current_tasks
            raw_resp = _bg_state._current_raw_response
            return status, headers, response_body, tasks, raw_resp
        finally:
            # If dispatch raised before drain ran, close any gens with throw
            # so their finally blocks still execute.
            leftover = _bg_state._current_yield_gens
            if leftover:
                from . import _routing as _r
                try:
                    _r.drain_yield_deps()
                except Exception:
                    pass
            _bg_state._current_tasks = None
            _bg_state._current_request = None
            _bg_state._current_raw_response = None
            _bg_state._current_yield_gens = []


__all__ = ["FastAPI"]
