"""Route plan compiler — runs at decorator time, builds a `ParamPlan` from
the handler's Python signature so the Rust dispatch can extract + cast params
without re-introspecting on each request.

Phase B-2: path params (int/float/str/bool/UUID + catch-all).
Phase B-3: query/header/cookie + validators (gt/le/min_length/pattern) + alias
           + list params.
Phase C-1: body params (Pydantic BaseModel, dict, list[Model]) with embed
           detection + multi-body auto-envelope.

Each plan entry is a dict with keys:
    {
        "name":       Python parameter name,
        "source":     "path" | "query" | "header" | "cookie" | "body" | "depends"
                      | "form" | "file",
        "type":       "int" | "float" | "str" | "bool" | "uuid" | "list[str]"
                      | "list[int]" | "any" | "model" | "raw" | "list[model]",
        "default":    Python value or None,
        "alias":      external name (for headers, query) or None,
        "required":   bool — true if no default was provided,
        "validators": dict with gt/ge/lt/le/min_length/max_length/pattern,
        "convert_underscores": bool — for headers, default True,
        "model":      Python class for Pydantic models (only for body sources),
        "embed":      bool — Body(embed=True) wraps body under {<name>: value},
        "media_type": e.g. "application/json", "application/x-www-form-urlencoded",
    }
"""

from __future__ import annotations

import inspect
import re
from io import BytesIO
from typing import Annotated, Any, get_args, get_origin, get_type_hints
from uuid import UUID

# Path placeholder regex.
_PATH_PARAM_RE = re.compile(r"\{([^}:]+)(?::[^}]+)?\}")


def parse_form_body(body: bytes, content_type: str) -> dict[str, list[Any]]:
    """Parse a request body as form data. Returns dict[str, list[value]] where
    each value is a `str` (urlencoded fields, simple multipart fields) or a
    Starlette `UploadFile` (multipart file fields).

    Supports `application/x-www-form-urlencoded` and `multipart/form-data`. For
    Phase C-4 we use a small hand-rolled multipart parser; Phase J swaps to
    Rust `multer` for hot-path speedup.
    """
    ct = (content_type or "").lower()
    if ct.startswith("application/x-www-form-urlencoded"):
        from urllib.parse import parse_qs
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            text = body.decode("latin-1")
        return parse_qs(text, keep_blank_values=True)
    if ct.startswith("multipart/form-data"):
        return _parse_multipart(body, content_type)
    return {}


def _parse_multipart(body: bytes, content_type: str) -> dict[str, list[Any]]:
    """Hand-rolled multipart parser sufficient for FastAPI's test surface.
    Each form field is a list of str or UploadFile values. File fields produce
    `starlette.datastructures.UploadFile` instances backed by BytesIO.
    """
    from starlette.datastructures import UploadFile, Headers as _Headers

    boundary_match = re.search(r"boundary=(.+)", content_type)
    if not boundary_match:
        return {}
    boundary = boundary_match.group(1).strip()
    if boundary.startswith('"') and boundary.endswith('"'):
        boundary = boundary[1:-1]
    boundary_bytes = b"--" + boundary.encode("latin-1")

    parts = body.split(boundary_bytes)
    out: dict[str, list[Any]] = {}
    for part in parts[1:-1]:  # skip preamble + trailing closer
        # Strip leading CRLF
        if part.startswith(b"\r\n"):
            part = part[2:]
        elif part.startswith(b"\n"):
            part = part[1:]
        # Strip trailing CRLF
        if part.endswith(b"\r\n"):
            part = part[:-2]
        elif part.endswith(b"\n"):
            part = part[:-1]
        # Split headers / content by CRLF CRLF or LF LF
        sep_idx = part.find(b"\r\n\r\n")
        sep_len = 4
        if sep_idx == -1:
            sep_idx = part.find(b"\n\n")
            sep_len = 2
        if sep_idx == -1:
            continue
        header_block = part[:sep_idx].decode("latin-1")
        content = part[sep_idx + sep_len :]

        headers: dict[str, str] = {}
        for line in header_block.split("\r\n" if "\r\n" in header_block else "\n"):
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()

        disposition = headers.get("content-disposition", "")
        name_match = re.search(r'name="([^"]*)"', disposition)
        if not name_match:
            continue
        name = name_match.group(1)

        filename_match = re.search(r'filename="([^"]*)"', disposition)
        if filename_match:
            filename = filename_match.group(1)
            file_ct = headers.get("content-type", "application/octet-stream")
            up = UploadFile(
                file=BytesIO(content),
                size=len(content),
                filename=filename,
                headers=_Headers(raw=[(b"content-type", file_ct.encode("latin-1"))]),
            )
            out.setdefault(name, []).append(up)
        else:
            try:
                value = content.decode("utf-8")
            except UnicodeDecodeError:
                value = content.decode("latin-1")
            out.setdefault(name, []).append(value)

    return out


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


def _is_pydantic_model(t: Any) -> bool:
    """True if `t` is a class that subclasses pydantic.BaseModel."""
    try:
        from pydantic import BaseModel
        return isinstance(t, type) and issubclass(t, BaseModel)
    except Exception:
        return False


def _type_kind(t: Any) -> str:
    """Map a Python type to our kind string. Returns "any" for unrecognized.

    Phase C-1: also returns "model" for Pydantic BaseModel subclasses, "raw" for
    bare dict/Any (used as Body() targets), and "list[model]" for `list[Model]`.
    """
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
        if args and _is_pydantic_model(args[0]):
            return "list[model]"
        return "list[str]"

    if inner is int:
        return "int"
    if inner is float:
        return "float"
    if inner is bool:
        return "bool"
    if inner is UUID:
        return "uuid"
    if inner is bytes:
        return "bytes"
    if inner is str or inner is inspect.Parameter.empty:
        return "str"
    if _is_pydantic_model(inner):
        return "model"
    # dict / Mapping / Any → raw JSON body if used in body position.
    if inner is dict or origin is dict:
        return "raw"
    return "any"


def _resolve_model_class(t: Any) -> Any | None:
    """Pull the Pydantic model class (or list element class) out of an annotation.

    Handles `Optional[Model]`, `Annotated[Model, ...]`, `list[Model]`, etc.
    Returns None if no model class is found.
    """
    inner, _opt = _unwrap_optional(t)
    inner, _ = _unwrap_annotated(inner)
    inner, _ = _unwrap_optional(inner)
    origin = get_origin(inner)
    if origin in (list, tuple):
        args = get_args(inner)
        if args and _is_pydantic_model(args[0]):
            return args[0]
        return None
    if _is_pydantic_model(inner):
        return inner
    return None


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
      4. Else, fall back: Pydantic BaseModel/list[Model]/dict → body (JSON);
         primitives → query.

    Post-pass: if the plan contains 2+ body entries, mark each as `embed=True`
    so they're looked up under their param name in the JSON envelope. Same for
    a single body marked `Body(embed=True)`.
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
        model_class = _resolve_model_class(type_for_kind)

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
            if type_kind in ("model", "list[model]", "raw"):
                source = "body"
            elif type_kind in ("int", "float", "bool", "str", "uuid", "any",
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

        # Body-only metadata.
        embed = bool(marker_kwargs.get("embed", False))
        media_type = marker_kwargs.get("media_type")
        if media_type is None:
            if source == "form":
                media_type = "application/x-www-form-urlencoded"
            elif source == "file":
                media_type = "multipart/form-data"
            else:
                media_type = "application/json"

        entry = {
            "name": name,
            "source": source,
            "type": type_kind,
            "default": default,
            "alias": alias,
            "required": required,
            "validators": _build_validators(marker_kwargs),
            "convert_underscores": convert_underscores,
            "embed": embed,
            "media_type": media_type,
        }
        if model_class is not None and source in ("body", "form"):
            entry["model"] = model_class
        plan.append(entry)

    # Post-pass: multi-body auto-embed. If 2+ body params (and not already
    # embedded as form/file), every JSON body param is wrapped under its name.
    body_indices = [i for i, e in enumerate(plan) if e["source"] == "body"]
    if len(body_indices) > 1:
        for i in body_indices:
            plan[i]["embed"] = True

    # Post-pass: UploadFile detection. A bare `file: UploadFile` parameter is
    # treated as a File() upload even without an Annotated marker. Same for
    # `list[UploadFile]`.
    for entry in plan:
        if entry["source"] != "query":
            continue
        ann_type = entry.get("type")
        # Already handled via marker; only fall-back-to-query primitives reach here.
        # We need the original annotation to detect UploadFile — but we've already
        # erased it. Simpler: re-walk hints for query-source entries to check.
    # Re-iterate signature for UploadFile detection (cheap; happens once at startup).
    try:
        from starlette.datastructures import UploadFile as _UploadFile
    except Exception:
        _UploadFile = None
    if _UploadFile is not None:
        for entry in plan:
            if entry["source"] not in ("query", "file"):
                continue
            name = entry["name"]
            ann = hints.get(name, sig.parameters[name].annotation)
            inner_ann, _ = _unwrap_annotated(ann)
            type_for_kind, _ = _unwrap_optional(inner_ann)
            origin = get_origin(type_for_kind)
            if isinstance(type_for_kind, type) and issubclass(type_for_kind, _UploadFile):
                entry["source"] = "file"
                entry["type"] = "uploadfile"
            elif origin in (list, tuple):
                args = get_args(type_for_kind)
                if args and isinstance(args[0], type) and issubclass(args[0], _UploadFile):
                    entry["source"] = "file"
                    entry["type"] = "list[uploadfile]"

    return plan
