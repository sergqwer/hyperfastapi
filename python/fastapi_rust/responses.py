"""Response classes — re-exported from Starlette.

Phase A: direct re-export. Phase C will replace with Rust-backed versions
that take advantage of zero-copy `Bytes` and lazy serialization.
"""

from __future__ import annotations

from starlette.responses import (
    FileResponse as FileResponse,
    HTMLResponse as HTMLResponse,
    JSONResponse as JSONResponse,
    PlainTextResponse as PlainTextResponse,
    RedirectResponse as RedirectResponse,
    Response as Response,
    StreamingResponse as StreamingResponse,
)

__all__ = [
    "FileResponse",
    "HTMLResponse",
    "JSONResponse",
    "PlainTextResponse",
    "RedirectResponse",
    "Response",
    "StreamingResponse",
]
