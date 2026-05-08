"""TestClient — re-export from Starlette.

Phase A: direct re-export. Phase B replaces with a Rust-backed in-process
client that drives the dispatch loop directly without going over a TCP socket.
"""

from __future__ import annotations

from starlette.testclient import TestClient as TestClient

__all__ = ["TestClient"]
