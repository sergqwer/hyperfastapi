"""fastapi_rust — Rust port of FastAPI (sibling package).

Phase A: only the import contract is in place. FastAPI() and APIRouter() store
route metadata via the Rust core but no actual dispatch happens yet — that's
Phase B. Other exports are re-exports from Starlette (which FastAPI itself
depends on) or thin Python stubs scheduled for Rust replacement in later phases.
"""

from __future__ import annotations

__version__ = "0.1.0"

# ---- Starlette re-exports (FastAPI re-exports these too) ------------------
from starlette import status as status
from starlette.background import BackgroundTasks as BackgroundTasks
from starlette.datastructures import UploadFile as UploadFile
from starlette.requests import Request as Request
from starlette.responses import Response as Response
from starlette.websockets import (
    WebSocket as WebSocket,
    WebSocketDisconnect as WebSocketDisconnect,
)

# ---- FastAPI-specific exceptions (subclass Starlette's) -------------------
from .exceptions import (
    HTTPException as HTTPException,
    WebSocketException as WebSocketException,
)

# ---- FastAPI core: Rust extension + Python ASGI bridge --------------------
# The Python `FastAPI` subclass adds the `async __call__(scope, receive, send)`
# method so Starlette's TestClient and uvicorn can drive it. The Rust dispatch
# logic is inherited from `_core.FastAPI`.
from .applications import FastAPI as FastAPI
from ._core import (
    APIRouter as APIRouter,
    Body as Body,
    Cookie as Cookie,
    Depends as Depends,
    File as File,
    Form as Form,
    Header as Header,
    Path as Path,
    Query as Query,
    Security as Security,
)

__all__ = [
    "__version__",
    "status",
    "FastAPI",
    "APIRouter",
    "BackgroundTasks",
    "UploadFile",
    "HTTPException",
    "WebSocketException",
    "Body",
    "Cookie",
    "Depends",
    "File",
    "Form",
    "Header",
    "Path",
    "Query",
    "Security",
    "Request",
    "Response",
    "WebSocket",
    "WebSocketDisconnect",
]
