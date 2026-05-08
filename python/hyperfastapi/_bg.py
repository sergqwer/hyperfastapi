"""Per-request state stash — used by the ASGI layer to hand things into the
Rust dispatcher's resolve_dependencies path that aren't part of the existing
arg list (BackgroundTasks, the Starlette Request).

Phase J replaces this side-channel by threading these objects through the
Rust ``_dispatch`` return / args directly.
"""

from __future__ import annotations

from typing import Any

_current_tasks: Any = None
_current_request: Any = None
# Phase F+: when a handler returns a StreamingResponse or FileResponse, the
# Rust dispatcher stashes the raw Response object here and the ASGI layer
# drives it via ``await response(scope, receive, send)`` instead of trying to
# unpack the body bytes.
_current_raw_response: Any = None

# Phase L: live yield-dep generators (sync + async) for the in-flight request.
# Pushed by resolve_dependencies as each yield-dep advances past its first
# yield; drained in LIFO order by drain_yield_deps after the handler runs
# (success → next(); exception → gen.throw() so the dep's except/finally fire).
_current_yield_gens: list = []


def reset(request: Any = None) -> None:
    """Phase N: single-call per-request state reset. Replaces 4 separate
    setattr() calls from Rust hot path with one function call (3 GIL-side
    attribute writes saved per request)."""
    global _current_tasks, _current_request, _current_raw_response, _current_yield_gens
    _current_tasks = None
    _current_request = request
    _current_raw_response = None
    _current_yield_gens = []
