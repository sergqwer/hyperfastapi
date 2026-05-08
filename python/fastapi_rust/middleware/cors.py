"""CORS middleware — re-export from Starlette."""

from __future__ import annotations

from starlette.middleware.cors import CORSMiddleware as CORSMiddleware

__all__ = ["CORSMiddleware"]
