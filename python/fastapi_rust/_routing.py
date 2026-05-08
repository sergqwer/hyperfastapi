"""Route plan compiler — runs at decorator time, builds a `ParamPlan` from
the handler's Python signature so the Rust dispatch can extract + cast params
without re-introspecting on each request.

Phase B-2 covers path params (int/float/str/bool/UUID + catch-all). Phase B-3
adds query/header/cookie. Phase C adds body. Phase D adds Depends/Security.
"""

from __future__ import annotations

import inspect
import re
from typing import Annotated, Any, get_args, get_origin, get_type_hints
from uuid import UUID


# Path placeholder syntax: `{name}` (any type), `{name:path}` (catch-all),
# `{name:int}` and `{name:float}` are NOT special (FastAPI uses annotation,
# not placeholder type, to drive casting). We only extract names.
_PATH_PARAM_RE = re.compile(r"\{([^}:]+)(?::[^}]+)?\}")


def extract_path_param_names(path_template: str) -> list[str]:
    """Return the names of all `{name}` placeholders in path order."""
    return _PATH_PARAM_RE.findall(path_template)


def is_catch_all(path_template: str, name: str) -> bool:
    """Detect `{name:path}` — the catch-all that captures slashes."""
    pattern = re.compile(r"\{" + re.escape(name) + r":path\}")
    return bool(pattern.search(path_template))


# Type → kind_str mapping for path params.
def _kind_for_path_type(annotation: Any, catch_all: bool) -> str:
    """Map a Python type annotation to the kind string the Rust dispatch uses
    for casting. Returns "path:int" / "path:float" / "path:str" / "path:bool"
    / "path:uuid" / "path:any" (catch-all defaults to str).
    """
    if catch_all:
        # Catch-all is always treated as str even if annotated otherwise —
        # FastAPI does this; users who want int catch-alls cast inside the handler.
        return "path:any"
    inner = _unwrap_annotated(annotation)
    if inner is int:
        return "path:int"
    if inner is float:
        return "path:float"
    if inner is bool:
        return "path:bool"
    if inner is UUID:
        return "path:uuid"
    if inner is str or inner is inspect.Parameter.empty:
        return "path:str"
    # Fallback — pass through as string and let the handler decide.
    return "path:str"


def _unwrap_annotated(annotation: Any) -> Any:
    """Strip `Annotated[T, ...]` to get T."""
    if get_origin(annotation) is Annotated:
        args = get_args(annotation)
        return args[0] if args else annotation
    return annotation


def compile_route_plan(handler: Any, path_template: str) -> list[tuple[str, str]]:
    """Inspect `handler` and return a list of `(param_name, kind_str)`.

    Phase B-2 only emits entries for path params (matching `{name}` in the
    template). Phase B-3 adds query/header/cookie (parameters with non-Path
    markers or simple primitive defaults).

    The Rust dispatch uses `kind_str` to know how to extract + cast the
    incoming raw string. Unknown kinds are skipped (the handler still runs,
    but with no value for that param — Phase B-3 fills in).
    """
    path_names = set(extract_path_param_names(path_template))
    plan: list[tuple[str, str]] = []
    try:
        sig = inspect.signature(handler)
    except (ValueError, TypeError):
        return plan

    try:
        hints = get_type_hints(handler, include_extras=True)
    except Exception:
        hints = {}

    for name, param in sig.parameters.items():
        if name in path_names:
            ann = hints.get(name, param.annotation)
            catch_all = is_catch_all(path_template, name)
            kind = _kind_for_path_type(ann, catch_all)
            plan.append((name, kind))
        # Phase B-3 will append query/header/cookie entries here.

    return plan
