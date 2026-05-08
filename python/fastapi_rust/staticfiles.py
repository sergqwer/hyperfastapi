"""StaticFiles — re-export from Starlette.

Phase A: re-export. Phase I optionally adds a Rust-backed variant for the
hot path of static-asset serving with traversal protection.
"""

from __future__ import annotations

from starlette.staticfiles import StaticFiles as StaticFiles

__all__ = ["StaticFiles"]
