"""BackgroundTasks state stash — Rust dispatch sets ``_current_tasks`` if the
handler declared a ``BackgroundTasks`` parameter; the ASGI layer reads it after
the response body is sent and runs the queued tasks.

Phase J replaces this side-channel by threading the tasks object through the
return tuple from Rust ``_dispatch`` directly.
"""

from __future__ import annotations

from typing import Any

_current_tasks: Any = None
