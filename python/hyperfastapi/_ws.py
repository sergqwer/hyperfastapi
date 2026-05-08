"""Native-WebSocket Python bridge.

The Rust side (`crates/fr-pyiface/src/ws.rs`) handles the TCP-level frame
protocol. This module exposes a Starlette-compatible WebSocket facade that
the user's `async def ws_handler(ws):` interacts with — the actual byte-level
push/pull goes through `_native_create` and `WsSendHandle`.

Frames produced by Rust are dicts:
    {"kind": "text",       "data": str}
    {"kind": "bytes",      "data": bytes}
    {"kind": "disconnect"}
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from . import _routing as _routing_mod


class WebSocketDisconnect(Exception):
    """Raised when the peer closes the connection — matches Starlette's API."""

    def __init__(self, code: int = 1005, reason: str = "") -> None:
        super().__init__(f"websocket disconnect (code={code})")
        self.code = code
        self.reason = reason


class NativeWebSocket:
    """A Starlette-shaped WebSocket implemented over the Rust hyper server.

    Methods follow the Starlette WebSocket contract:
      - ``await ws.accept()``                       (no-op; Rust already 101'd)
      - ``await ws.send_text(str)``
      - ``await ws.send_bytes(bytes)``
      - ``await ws.send_json(obj)``
      - ``await ws.receive_text()``  -> str
      - ``await ws.receive_bytes()`` -> bytes
      - ``await ws.receive_json()``  -> Any
      - ``await ws.close(code=1000, reason="")``
      - ``async for msg in ws.iter_text():``  -> str
    """

    __slots__ = ("_send", "_queue", "_closed")

    def __init__(self, send_handle: Any, queue: asyncio.Queue) -> None:
        # send_handle: hyperfastapi._core.WsSendHandle (Rust PyClass)
        self._send = send_handle
        self._queue = queue
        self._closed = False

    async def accept(self, subprotocol: str | None = None, headers: Any = None) -> None:
        # The Rust side already sent the 101 — nothing more to do here.
        return None

    async def send_text(self, data: str) -> None:
        if self._closed:
            raise WebSocketDisconnect()
        self._send.send_text(data)

    async def send_bytes(self, data: bytes) -> None:
        if self._closed:
            raise WebSocketDisconnect()
        self._send.send_bytes(data)

    async def send_json(self, data: Any, mode: str = "text") -> None:
        encoded = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
        if mode == "binary":
            await self.send_bytes(encoded.encode("utf-8"))
        else:
            await self.send_text(encoded)

    async def _next(self) -> dict:
        msg = await self._queue.get()
        if msg["kind"] == "disconnect":
            self._closed = True
            raise WebSocketDisconnect()
        return msg

    async def receive_text(self) -> str:
        msg = await self._next()
        if msg["kind"] != "text":
            raise RuntimeError(f"expected text frame, got {msg['kind']}")
        return msg["data"]

    async def receive_bytes(self) -> bytes:
        msg = await self._next()
        if msg["kind"] != "bytes":
            raise RuntimeError(f"expected bytes frame, got {msg['kind']}")
        return msg["data"]

    async def receive_json(self) -> Any:
        msg = await self._next()
        if msg["kind"] == "text":
            return json.loads(msg["data"])
        if msg["kind"] == "bytes":
            return json.loads(msg["data"].decode("utf-8"))
        raise RuntimeError(f"expected text/bytes frame, got {msg['kind']}")

    async def close(self, code: int = 1000, reason: str = "") -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._send.close(code, reason)
        except RuntimeError:
            pass

    # ---- Starlette-style iterators ----------------------------------------

    async def iter_text(self):
        try:
            while True:
                yield await self.receive_text()
        except WebSocketDisconnect:
            return

    async def iter_bytes(self):
        try:
            while True:
                yield await self.receive_bytes()
        except WebSocketDisconnect:
            return

    async def iter_json(self):
        try:
            while True:
                yield await self.receive_json()
        except WebSocketDisconnect:
            return


def _native_create(handler: Any, send_handle: Any):
    """Called from Rust just after the WebSocket handshake completes.

    Spawns the user's async handler coroutine on the persistent worker loop,
    passing it a ``NativeWebSocket`` instance. Returns the asyncio.Queue that
    Rust should push received frames into.
    """
    loop = _routing_mod._ensure_worker_loop()
    queue: asyncio.Queue = asyncio.Queue(maxsize=0)
    ws = NativeWebSocket(send_handle, queue)

    async def _runner() -> None:
        try:
            await handler(ws)
        except WebSocketDisconnect:
            pass
        except Exception:
            # Best effort: log + close.
            import traceback
            traceback.print_exc()
        finally:
            try:
                await ws.close(code=1000)
            except Exception:
                pass

    asyncio.run_coroutine_threadsafe(_runner(), loop)
    return queue


def _native_push_frame(queue: asyncio.Queue, frame: dict) -> None:
    """Cross-thread push from Rust to the asyncio.Queue. Schedules
    ``queue.put_nowait`` on the queue's owning event loop via
    ``call_soon_threadsafe``."""
    loop = _routing_mod._ensure_worker_loop()
    loop.call_soon_threadsafe(queue.put_nowait, frame)
