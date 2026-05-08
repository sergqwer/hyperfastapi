"""Trusted-host middleware — re-export from Starlette."""

from __future__ import annotations

from starlette.middleware.trustedhost import TrustedHostMiddleware as TrustedHostMiddleware

__all__ = ["TrustedHostMiddleware"]
