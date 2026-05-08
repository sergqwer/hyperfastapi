"""Exception types — subclass Starlette's so middleware that catches the
parent class catches ours too.
"""

from __future__ import annotations

from typing import Any, Mapping

from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.exceptions import WebSocketException as StarletteWebSocketException


class HTTPException(StarletteHTTPException):
    """FastAPI-compatible HTTP exception. Subclasses Starlette's so generic
    handlers catch both — replicates `from fastapi import HTTPException`.
    """

    def __init__(
        self,
        status_code: int,
        detail: Any = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(status_code=status_code, detail=detail, headers=headers)


class WebSocketException(StarletteWebSocketException):
    """FastAPI-compatible WebSocket exception."""

    def __init__(self, code: int, reason: str | None = None) -> None:
        super().__init__(code=code, reason=reason)


class RequestValidationError(ValueError):
    """Raised when Pydantic validation fails for an incoming request.

    Phase A: minimal — has `.errors()` returning the supplied error list.
    Phase C wires this into the dispatch pipeline so 422 responses use the
    standard `{"detail": [...]}` format.
    """

    def __init__(self, errors: list[Any], body: Any = None) -> None:
        self._errors = list(errors)
        self.body = body
        super().__init__(self._errors)

    def errors(self) -> list[Any]:
        return list(self._errors)
