"""WebSocket — accept, send/receive text/bytes/json, close, disconnect."""

from __future__ import annotations

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.testclient import TestClient

app = FastAPI()


@app.websocket("/echo")
async def ws_echo(ws: WebSocket) -> None:
    await ws.accept()
    try:
        while True:
            data = await ws.receive_text()
            await ws.send_text(f"echo: {data}")
    except WebSocketDisconnect:
        pass


@app.websocket("/echo-bytes")
async def ws_echo_bytes(ws: WebSocket) -> None:
    await ws.accept()
    data = await ws.receive_bytes()
    await ws.send_bytes(b"got: " + data)


@app.websocket("/echo-json")
async def ws_echo_json(ws: WebSocket) -> None:
    await ws.accept()
    data = await ws.receive_json()
    await ws.send_json({"received": data})


@app.websocket("/close-1008")
async def ws_close(ws: WebSocket) -> None:
    await ws.accept()
    await ws.close(code=1008, reason="policy violation")


client = TestClient(app)


def test_ws_text_echo() -> None:
    with client.websocket_connect("/echo") as ws:
        ws.send_text("hello")
        msg = ws.receive_text()
        assert msg == "echo: hello"


def test_ws_text_echo_multiple_messages() -> None:
    with client.websocket_connect("/echo") as ws:
        ws.send_text("one")
        assert ws.receive_text() == "echo: one"
        ws.send_text("two")
        assert ws.receive_text() == "echo: two"


def test_ws_bytes_echo() -> None:
    with client.websocket_connect("/echo-bytes") as ws:
        ws.send_bytes(b"binary-data")
        assert ws.receive_bytes() == b"got: binary-data"


def test_ws_json_echo() -> None:
    with client.websocket_connect("/echo-json") as ws:
        ws.send_json({"name": "Alice", "n": 42})
        assert ws.receive_json() == {"received": {"name": "Alice", "n": 42}}


def test_ws_server_initiated_close_with_code() -> None:
    """Server-initiated close should propagate the close code to the client."""
    import pytest

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/close-1008") as ws:
            ws.receive_text()  # raises WebSocketDisconnect
    assert exc_info.value.code == 1008


def test_ws_disconnect_propagates_to_handler() -> None:
    """Client closing the websocket should be observable on the server side
    (via WebSocketDisconnect). We verify by reusing the echo route — opening
    and closing without errors implies the handler completed normally.
    """
    with client.websocket_connect("/echo") as ws:
        ws.send_text("ping")
        assert ws.receive_text() == "echo: ping"
        # Exit context manager triggers close → WebSocketDisconnect on server
    # If handler had crashed on disconnect, TestClient would have raised here.
