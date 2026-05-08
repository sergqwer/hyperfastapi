"""WebSocket-only perf app launched by ws_bench.py via run_native()."""

from __future__ import annotations

import os
import sys

if os.environ.get("HYPERFASTAPI_AS_FASTAPI") == "1":
    import hyperfastapi  # noqa: F401

    sys.modules["fastapi"] = hyperfastapi

from hyperfastapi import FastAPI
from hyperfastapi._ws import WebSocketDisconnect

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.websocket("/echo")
async def ws_echo(ws) -> None:
    """Echo every text frame back, untyped — the canonical WebSocket bench
    target. The handler stays simple so the benchmark measures framework
    overhead, not user logic."""
    await ws.accept()
    try:
        while True:
            text = await ws.receive_text()
            await ws.send_text(text)
    except WebSocketDisconnect:
        return
