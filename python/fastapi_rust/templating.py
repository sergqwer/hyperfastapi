"""Jinja2 templates — re-export from Starlette.

Phase A: re-export. Templates live entirely in Python (no Rust win for the
Jinja runtime), so this is unlikely to change.
"""

from __future__ import annotations

try:
    from starlette.templating import Jinja2Templates as Jinja2Templates
except ImportError:
    Jinja2Templates = None  # jinja2 is an optional install

__all__ = ["Jinja2Templates"]
