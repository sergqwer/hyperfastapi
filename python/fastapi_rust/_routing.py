"""Route plan compiler — runs at decorator time, builds a `ParamPlan` from
the handler's Python signature so the Rust dispatch can extract + cast params
without re-introspecting on each request.

Phase B-2: path params (int/float/str/bool/UUID + catch-all).
Phase B-3: query/header/cookie + validators (gt/le/min_length/pattern) + alias
           + list params.

Each plan entry is a dict with keys:
    {
        "name":       Python parameter name,
        "source":     "path" | "query" | "header" | "cookie" | "body" | "depends",
        "type":       "int" | "float" | "str" | "bool" | "uuid" | "list[str]"
                      | "list[int]" | "any" | "model:<class_name>",
        "default":    Python value or None (None means required IF "required" is True),
        "alias":      external name (for headers, query) or None,
        "required":   bool — true if no default was provided,
        "validators": dict with gt/ge/lt/le/min_length/max_length/pattern,
        "convert_underscores": bool — for headers, default True,
    }
"""

from __future__ import annotations

import inspect
import re
from typing import Annotated, Any, get_args, get_origin, get_type_hints
from uuid import UUID

# Path placeholder regex.
_PATH_PARAM_RE = re.compile(r"\{([^}:]+)(?::[^}]+)?\}")


def extract_path_param_names(path_template: str) -> list[str]:
    return _PATH_PARAM_RE.findall(path_template)


def is_catch_all(path_template: str, name: str) -> bool:
    return bool(re.search(r"\{" + re.escape(name) + r":path\}", path_template))


def _unwrap_annotated(annotation: Any) -> tuple[Any, list[Any]]:
    """Recursively peel `Annotated[T, M]` and `Optional[T]`/`Union[T, None]`
    layers, accumulating Annotated metadata across layers.

    Critical: a parameter declared as `x: Annotated[str | None, Cookie()] =
    None` is rewritten by Python's typing as `Optional[Annotated[...]]` —
    `get_origin` then returns `Union`, not `Annotated`, and a naive single-
    layer peel loses the Cookie() metadata.
    """
    import typing
    metadata: list[Any] = []
    seen: set[int] = set()
    cur = annotation
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        origin = get_origin(cur)
        if origin is Annotated:
            args = get_args(cur)
            if args:
                metadata.extend(args[1:])
                cur = args[0]
                continue
        # Optional[X] / Union[X, None] / X | None
        is_union = origin is typing.Union
        if not is_union:
            try:
                import types as _types
                if hasattr(_types, "UnionType") and isinstance(cur, _types.UnionType):
                    is_union = True
            except Exception:
                pass
        if is_union:
            args = get_args(cur)
            non_none = [a for a in args if a is not type(None)]
            if len(non_none) == 1:
                cur = non_none[0]
                continue
        break
    return cur, metadata


def _unwrap_optional(t: Any) -> tuple[Any, bool]:
    """`Optional[T]` aka `Union[T, None]` aka `T | None` → (T, True). Else → (t, False)."""
    origin = get_origin(t)
    if origin is None:
        return t, False
    # typing.Union and types.UnionType (3.10+ X | Y syntax)
    args = get_args(t)
    if not args:
        return t, False
    # Filter None
    non_none = [a for a in args if a is not type(None)]
    if len(non_none) == len(args):
        return t, False  # not optional
    if len(non_none) == 1:
        return non_none[0], True
    return t, True  # complex union — treat as optional but type stays as-is


def _type_kind(t: Any) -> str:
    """Map a Python type to our kind string. Returns "any" for unrecognized."""
    inner, _opt = _unwrap_optional(t)
    inner, _ = _unwrap_annotated(inner)
    inner, _ = _unwrap_optional(inner)

    # Handle list[T]
    origin = get_origin(inner)
    if origin in (list, tuple):
        args = get_args(inner)
        if args and args[0] is int:
            return "list[int]"
        if args and args[0] is float:
            return "list[float]"
        return "list[str]"

    if inner is int:
        return "int"
    if inner is float:
        return "float"
    if inner is bool:
        return "bool"
    if inner is UUID:
        return "uuid"
    if inner is str or inner is inspect.Parameter.empty:
        return "str"
    return "any"


def _is_marker(obj: Any, marker_name: str) -> bool:
    """Check whether `obj` is an instance of the named marker (Body/Query/...).
    Imports are deferred so this works even before _core is loaded.
    """
    try:
        mod = __import__("fastapi_rust._core", fromlist=[marker_name])
        cls = getattr(mod, marker_name, None)
        return cls is not None and isinstance(obj, cls)
    except Exception:
        return False


def _extract_marker_kwargs(marker: Any) -> dict[str, Any]:
    """Pull validator kwargs out of a Path/Query/Header/... instance."""
    kwargs_obj = getattr(marker, "kwargs", None)
    if kwargs_obj is None:
        return {}
    try:
        return dict(kwargs_obj)
    except Exception:
        return {}


_VALIDATOR_KEYS = {"gt", "ge", "lt", "le", "min_length", "max_length", "pattern"}


def _build_validators(kwargs: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in kwargs.items() if k in _VALIDATOR_KEYS}


def _marker_from_metadata(metadata: list[Any]) -> tuple[str | None, Any]:
    """Look through Annotated metadata for a Body/Query/Path/Header/Cookie/Form/
    File/Depends/Security marker. Returns (source_name, marker_instance) or
    (None, None) if no marker present."""
    for m in metadata:
        for kind in ("Path", "Query", "Header", "Cookie", "Body", "Form", "File",
                     "Depends", "Security"):
            if _is_marker(m, kind):
                source = kind.lower()  # "path" / "query" / ...
                return source, m
    return None, None


def compile_route_plan(handler: Any, path_template: str) -> list[dict[str, Any]]:
    """Inspect handler signature and produce a list of param plan entries.

    Algorithm per param:
      1. If name matches a `{name}` placeholder in the path → source=path.
      2. Else, look at default value: if it's a Path/Query/... marker, use its
         source.
      3. Else, look at Annotated metadata for the same kinds of markers.
      4. Else, fall back: simple primitive → query (with default None),
         Pydantic BaseModel → body, otherwise skip.
    """
    path_names = set(extract_path_param_names(path_template))
    plan: list[dict[str, Any]] = []
    try:
        sig = inspect.signature(handler)
    except (ValueError, TypeError):
        return plan

    try:
        hints = get_type_hints(handler, include_extras=True)
    except Exception:
        hints = {}

    for name, param in sig.parameters.items():
        ann = hints.get(name, param.annotation)
        inner_ann, metadata = _unwrap_annotated(ann)
        # Optional -> mark required=False, peel one layer
        type_for_kind, optional = _unwrap_optional(inner_ann)
        type_kind = _type_kind(type_for_kind)

        default = None
        has_default = param.default is not inspect.Parameter.empty
        if has_default:
            default = param.default

        marker_kwargs: dict[str, Any] = {}
        alias: str | None = None
        convert_underscores = True
        source: str | None = None

        # Always pull marker metadata from Annotated (regardless of source) so
        # validators on Path() / Query() etc. apply even when source was
        # determined by path placeholder match.
        meta_source, meta_marker = _marker_from_metadata(metadata) if metadata else (None, None)

        # 1. Path placeholder
        if name in path_names:
            source = "path"

        # 2. Default-as-marker (older FastAPI style)
        if source is None and has_default:
            for kind in ("Path", "Query", "Header", "Cookie", "Body", "Form",
                         "File", "Depends", "Security"):
                if _is_marker(default, kind):
                    source = kind.lower()
                    marker_kwargs = _extract_marker_kwargs(default)
                    inner_default = getattr(default, "default", None)
                    has_default = inner_default is not None
                    default = inner_default
                    break

        # 3. Annotated[..., Marker(...)] — set source if not already.
        if source is None and meta_source is not None:
            source = meta_source

        # Pull validators from Annotated marker no matter what determined source.
        if meta_marker is not None and not marker_kwargs:
            marker_kwargs = _extract_marker_kwargs(meta_marker)

        # 4. Fallback by type
        if source is None:
            # Primitives → query; complex types → body (Phase C wires body,
            # so Phase B-3 just emits "query" and hopes for primitives).
            if type_kind in ("int", "float", "bool", "str", "uuid", "any",
                              "list[int]", "list[float]", "list[str]"):
                source = "query"
            else:
                # Unknown — skip, handler's Python defaults apply.
                continue

        # Header semantics: convert_underscores is from the marker kwargs;
        # default True; alias from kwargs.
        if source == "header":
            convert_underscores = bool(marker_kwargs.get("convert_underscores", True))
        alias = marker_kwargs.get("alias")

        # required: false if there's a default value of any kind, OR Optional[T].
        required = not (has_default or optional)

        plan.append(
            {
                "name": name,
                "source": source,
                "type": type_kind,
                "default": default,
                "alias": alias,
                "required": required,
                "validators": _build_validators(marker_kwargs),
                "convert_underscores": convert_underscores,
            }
        )

    return plan
