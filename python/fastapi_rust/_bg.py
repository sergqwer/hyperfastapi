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
