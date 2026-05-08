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
