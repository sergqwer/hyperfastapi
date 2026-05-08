"""StreamingResponse — chunked transfer, sync + async generators, media_type."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

app = FastAPI()


@app.get("/sync")
def sync_stream() -> StreamingResponse:
    def gen():
        yield b"chunk-1 "
        yield b"chunk-2 "
        yield b"chunk-3"

    return StreamingResponse(gen(), media_type="text/plain")


@app.get("/async")
def async_stream() -> StreamingResponse:
    async def gen():
        for i in range(3):
            yield f"async-{i} ".encode()

    return StreamingResponse(gen(), media_type="application/octet-stream")


@app.get("/lots")
def lots_of_data() -> StreamingResponse:
    """Many small chunks to verify the transfer doesn't lose any."""
    def gen():
        for i in range(100):
            yield f"{i:03d}-".encode()

    return StreamingResponse(gen(), media_type="text/plain")


client = TestClient(app)


def test_sync_generator_assembles_correctly() -> None:
    response = client.get("/sync")
    assert response.status_code == 200
    assert response.content == b"chunk-1 chunk-2 chunk-3"


def test_async_generator_assembles_correctly() -> None:
    response = client.get("/async")
    assert response.status_code == 200
    assert response.content == b"async-0 async-1 async-2 "


def test_streaming_media_type_propagates() -> None:
    response = client.get("/async")
    assert response.headers["content-type"] == "application/octet-stream"


def test_streaming_text_content_type() -> None:
    """text/* media types may get charset added."""
    response = client.get("/sync")
    assert response.headers["content-type"].startswith("text/plain")


def test_streaming_many_chunks_no_data_loss() -> None:
    response = client.get("/lots")
    assert response.status_code == 200
    expected = b"".join(f"{i:03d}-".encode() for i in range(100))
    assert response.content == expected
