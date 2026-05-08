"""Param marker classes — re-exported from the Rust extension.

Tests do `import fastapi.params as fastapi_params; isinstance(Query(None),
fastapi_params.Query)`. We expose the same Rust class under both
`fastapi_rust.Query` and `fastapi_rust.params.Query` so the isinstance check
holds.
"""

from __future__ import annotations

from ._core import (
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
    "Body",
    "Cookie",
    "Depends",
    "File",
    "Form",
    "Header",
    "Path",
    "Query",
    "Security",
]
