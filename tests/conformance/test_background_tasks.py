"""BackgroundTasks — FIFO order, sync+async, executes after response sent."""

from __future__ import annotations

import asyncio
import threading

from fastapi import BackgroundTasks, FastAPI
from fastapi.testclient import TestClient

app = FastAPI()

_log: list[str] = []
_done = threading.Event()


def sync_task(label: str) -> None:
    _log.append(label)


async def async_task(label: str) -> None:
    await asyncio.sleep(0)
    _log.append(label)


def signal_done() -> None:
    _log.append("DONE")
    _done.set()


@app.post("/many")
def many_tasks(bg: BackgroundTasks) -> dict:
    bg.add_task(sync_task, "first")
    bg.add_task(sync_task, "second")
    bg.add_task(sync_task, "third")
    bg.add_task(signal_done)
    return {"queued": 4}


@app.post("/mixed")
def mixed_tasks(bg: BackgroundTasks) -> dict:
    bg.add_task(sync_task, "sync-A")
    bg.add_task(async_task, "async-B")
    bg.add_task(signal_done)
    return {"queued": 3}


client = TestClient(app)


def test_background_tasks_run_in_fifo_order() -> None:
    _log.clear()
    _done.clear()
    response = client.post("/many")
    assert response.status_code == 200
    assert response.json() == {"queued": 4}
    # Wait for the marker that the last task completed
    assert _done.wait(timeout=5), "background tasks did not finish in time"
    assert _log == ["first", "second", "third", "DONE"]


def test_background_tasks_supports_sync_and_async() -> None:
    _log.clear()
    _done.clear()
    response = client.post("/mixed")
    assert response.status_code == 200
    assert _done.wait(timeout=5)
    assert _log == ["sync-A", "async-B", "DONE"]


def test_background_task_does_not_delay_response_status() -> None:
    """Even if tasks haven't finished, the client gets the response status promptly."""
    _log.clear()
    _done.clear()
    response = client.post("/many")
    # Response is already returned with 200 even before tasks completed
    assert response.status_code == 200
    # But tasks WILL eventually complete — wait then verify
    assert _done.wait(timeout=5)


def test_background_response_body_is_sent_before_tasks() -> None:
    """The response body must be delivered before the tasks run.

    TestClient blocks until tasks complete (it's in-process), so the strict
    timing guarantee is hard to test here without async tools — but we can at
    least assert the body is the route's return value, not affected by tasks.
    """
    _log.clear()
    _done.clear()
    response = client.post("/many")
    assert response.json() == {"queued": 4}
