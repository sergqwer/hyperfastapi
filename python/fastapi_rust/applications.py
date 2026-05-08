"""ASGI bridge — Python subclass of the Rust `_core.FastAPI` PyClass.

Adds an `async def __call__(scope, receive, send)` so Starlette's TestClient
(and uvicorn / hypercorn / etc.) can drive our app. Internal dispatch goes
through the Rust `_dispatch(method, path, body)` method that returns
`(status, headers, body)` tuple.

Phase B-1: HTTP only, no WebSocket. Body is collected from `receive()` then
handed to Rust as bytes; Rust currently ignores it (no body params yet —
Phase B-2/C).
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Iterable

from ._core import FastAPI as _RustFastAPI

# ASGI types — kept as Any to avoid a hard dep on a typing pkg.
Scope = dict
Receive = Callable[[], Awaitable[dict]]
Send = Callable[[dict], Awaitable[None]]


class FastAPI(_RustFastAPI):
    """Sibling-package replacement for `fastapi.FastAPI`. Inherits all the
    Rust route-management methods from `_RustFastAPI` and adds an ASGI
    `__call__` so it works with Starlette's TestClient and uvicorn.
    """

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        scope_type = scope.get("type")

        # ---- Lifespan: minimal echo of the events back ------------------
        if scope_type == "lifespan":
            while True:
                msg = await receive()
                if msg["type"] == "lifespan.startup":
                    # Phase F runs user-registered on_startup handlers here.
                    await send({"type": "lifespan.startup.complete"})
                elif msg["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return

        # ---- HTTP --------------------------------------------------------
        if scope_type == "http":
            method: str = scope["method"]
            path: str = scope["path"]

            # Decode query_string (bytes) → str. ASGI guarantees URL-safe ASCII
            # encoding, so latin-1 round-trips bytes safely.
            query_bytes = scope.get("query_string") or b""
            query_string = (
                query_bytes.decode("latin-1")
                if isinstance(query_bytes, (bytes, bytearray))
                else str(query_bytes)
            )

            # Headers: ASGI delivers a list of (bytes, bytes) tuples. Decode to
            # (str, str) so the Rust extract path doesn't have to repeat this.
            raw_headers: Iterable[tuple[bytes, bytes]] = scope.get("headers") or []
            headers_list: list[tuple[str, str]] = [
                (k.decode("latin-1"), v.decode("latin-1")) for k, v in raw_headers
            ]

            # Drain the request body.
            body = bytearray()
            while True:
                msg = await receive()
                if msg.get("type") == "http.request":
                    body.extend(msg.get("body") or b"")
                    if not msg.get("more_body", False):
                        break
                elif msg.get("type") == "http.disconnect":
                    return

            try:
                status, headers, response_body = self._dispatch(
                    method, path, query_string, headers_list, bytes(body)
                )
            except Exception as exc:  # noqa: BLE001
                # Phase F adds proper exception_handlers; Phase B-1 falls
                # back to a 500 with the exception text so test failures
                # surface the underlying error rather than a hung connection.
                detail = repr(exc)
                response_body = (
                    b'{"detail":"Internal Server Error","error":'
                    + repr(detail).encode("utf-8")
                    + b"}"
                )
                status = 500
                headers = [("content-type", "application/json")]

            # Encode header list (str, str) → (bytes, bytes) per ASGI spec.
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
            return

        # ---- WebSocket: Phase G ----------------------------------------
        if scope_type == "websocket":
            # Reject by closing — Phase G implements proper handshake.
            await send({"type": "websocket.close", "code": 1011})
            return


__all__ = ["FastAPI"]
