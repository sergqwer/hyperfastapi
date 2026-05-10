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


_WORKER_LOOP = None
_WORKER_LOOP_LOCK = None


def _ensure_worker_loop():
    """Lazily start a single dedicated daemon thread running an asyncio event
    loop. All async coroutines submitted from sync dispatch land on this loop
    via run_coroutine_threadsafe. Persistent thread + persistent loop avoids
    paying the spawn-thread + create-loop cost per request — that was Phase J's
    biggest single async-handler perf hit (5x slowdown vs sync at ~2k RPS).
    """
    global _WORKER_LOOP, _WORKER_LOOP_LOCK
    if _WORKER_LOOP is not None:
        return _WORKER_LOOP

    import asyncio
    import threading

    if _WORKER_LOOP_LOCK is None:
        _WORKER_LOOP_LOCK = threading.Lock()
    with _WORKER_LOOP_LOCK:
        if _WORKER_LOOP is not None:
            return _WORKER_LOOP
        loop = asyncio.new_event_loop()
        ready = threading.Event()

        def runner() -> None:
            asyncio.set_event_loop(loop)
            ready.set()
            try:
                loop.run_forever()
            finally:
                loop.close()

        t = threading.Thread(target=runner, name="hyperfastapi-async-worker", daemon=True)
        t.start()
        ready.wait()
        _WORKER_LOOP = loop
    return _WORKER_LOOP


def _run_coro_blocking(coro):
    """Run a coroutine to completion from a sync context that may itself be
    running inside an event loop (e.g. Starlette TestClient).

    Submits to a long-lived worker loop running on a dedicated daemon thread,
    then blocks the caller on the resulting Future. This is correct from any
    thread (calling thread's running loop, if any, is irrelevant — the
    coroutine runs on the worker loop) and avoids the per-request spawn cost
    of the previous fresh-thread implementation.
    """
    import asyncio

    loop = _ensure_worker_loop()
    fut = asyncio.run_coroutine_threadsafe(coro, loop)
    return fut.result()


def call_with_async_handling(callable_, kwargs):
    """Invoke `callable_(**kwargs)` synchronously, transparently running
    async callables via `_run_coro_blocking` so dispatch stays sync.

    Phase L: yield-style deps (both sync generator and async generator) get
    their first yielded value returned to the caller; the live generator is
    pushed onto ``_bg._current_yield_gens`` so ``drain_yield_deps()`` can
    advance them through their finally/except blocks AFTER the handler has
    finished running.
    """
    import inspect as _ins
    is_async_fn = _ins.iscoroutinefunction(callable_) or (
        not _ins.isclass(callable_)
        and hasattr(callable_, "__call__")
        and _ins.iscoroutinefunction(callable_.__call__)
    )
    is_async_gen_fn = _ins.isasyncgenfunction(callable_) or (
        not _ins.isclass(callable_)
        and hasattr(callable_, "__call__")
        and _ins.isasyncgenfunction(callable_.__call__)
    )

    if is_async_gen_fn:
        from . import _bg as _bg_state
        agen = callable_(**kwargs)

        async def _first():
            return await agen.__anext__()

        first_value = _run_coro_blocking(_first())
        _bg_state._current_yield_gens.append(("async", agen))
        return first_value

    if is_async_fn:
        # Phase Q fast path: most FastAPI `async def` handlers don't actually
        # await anything (`async def f(): return {...}`). For those, calling
        # `coro.send(None)` raises StopIteration immediately with the return
        # value — no event loop, no cross-thread submission. Saves ~50µs/req
        # vs the worker-loop path. If the coroutine yields (real await), we
        # close the partial coro and re-run on the worker loop.
        #
        # Phase R+: SAFETY GUARD. The fast path drives the coroutine in pure
        # sync context. But ASGI invokes us from a thread that owns its own
        # asyncio loop (uvicorn's main loop). Inside coro.send(None) the user
        # may call `asyncio.create_task(...)`, `asyncio.Lock()`, etc. — those
        # call `get_running_loop()`, which on the ASGI thread returns
        # uvicorn's loop, NOT the persistent worker loop. The created tasks
        # then get pinned to uvicorn's loop. A subsequent request that goes
        # through the slow path (worker loop) tries to gather/await those
        # tasks → "got Future attached to a different loop". To avoid the
        # split, only take the fast path when no other loop is running on
        # this thread; otherwise the worker-loop path is the consistent
        # answer for every request.
        import asyncio as _aio
        try:
            _aio.get_running_loop()
            on_owned_loop_thread = True
        except RuntimeError:
            on_owned_loop_thread = False
        if not on_owned_loop_thread:
            coro = callable_(**kwargs)
            try:
                coro.send(None)
            except StopIteration as e:
                return e.value
            # Coroutine yielded — needs an event loop. Close the partial and
            # re-create. Cost: one extra handler call, dwarfed by the loop hop.
            try:
                coro.close()
            except Exception:
                pass
        return _run_coro_blocking(callable_(**kwargs))

    result = callable_(**kwargs)
    if _ins.isgenerator(result):
        from . import _bg as _bg_state
        first_value = next(result)
        _bg_state._current_yield_gens.append(("sync", result))
        return first_value
    return result


def drain_yield_deps(exc=None):
    """Advance every live yield-dep generator past its yield point.

    Called from Rust dispatch after the handler runs (with no exc), or after
    the handler raised (exc is not None). Walks ``_bg._current_yield_gens`` in
    LIFO order — last opened, first closed — matching FastAPI's contract.

    For sync generators: ``next(gen)`` runs everything after the yield (the
    finally block). If we have an ``exc``, ``gen.throw()`` injects it so the
    dep's ``except`` clause sees it before the finally.

    For async generators: same but via ``athrow`` / ``__anext__``, driven on
    the worker loop.
    """
    from . import _bg as _bg_state
    gens = _bg_state._current_yield_gens
    if not gens:
        return
    # Drain in reverse — last setup, first teardown.
    teardown_exc: BaseException | None = None
    while gens:
        kind, gen = gens.pop()
        try:
            if kind == "sync":
                if exc is not None:
                    try:
                        gen.throw(type(exc), exc, exc.__traceback__)
                    except StopIteration:
                        pass
                    except BaseException as raised:
                        # The dep re-raised (or raised something new). Keep
                        # going through the stack so each dep sees teardown,
                        # then re-raise the last exception we collected.
                        if raised is exc:
                            pass
                        else:
                            teardown_exc = raised
                else:
                    try:
                        next(gen)
                    except StopIteration:
                        pass
            else:  # async
                async def _step(g=gen, e=exc):
                    try:
                        if e is not None:
                            await g.athrow(type(e), e, e.__traceback__)
                        else:
                            await g.__anext__()
                    except (StopAsyncIteration, StopIteration):
                        return
                try:
                    _run_coro_blocking(_step())
                except BaseException as raised:
                    if raised is not exc:
                        teardown_exc = raised
        finally:
            try:
                if kind == "sync":
                    gen.close()
                else:
                    async def _aclose(g=gen):
                        try:
                            await g.aclose()
                        except Exception:
                            pass
                    try:
                        _run_coro_blocking(_aclose())
                    except Exception:
                        pass
            except Exception:
                pass
    if teardown_exc is not None and exc is None:
        raise teardown_exc


def extract_security_info(
    plan: list[dict[str, Any]],
    route_deps_markers: list[Any] | None = None,
) -> list[dict[str, Any]]:
    """Walk a route's plan (including nested dep_plans) and collect every
    security-class entry that contributes to the route's OpenAPI security
    requirement.

    Returns a list of dicts in registration order:
        [{"scheme_name": str, "scopes": list[str], "model": dict}, ...]

    Scopes carried by an outer ``Security(callable, scopes=[...])`` propagate
    as ambient scopes into the dep's sub-plan, so when an inner Depends(oauth2)
    is hit, the recorded scope list reflects the outer Security's scopes.

    Optional ``route_deps_markers``: route-level ``dependencies=[Depends(...)]``
    markers from the decorator. They are expanded into synthetic plan entries
    and walked together with the handler plan, so ``Security(check_dep,
    scopes=["read:items"])`` on the route surfaces in OpenAPI.
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    if route_deps_markers:
        synthetic = expand_route_level_dependencies(route_deps_markers)
        all_entries = list(synthetic) + list(plan)
    else:
        all_entries = list(plan)

    def walk(entries: list[dict[str, Any]], ambient: list[str]) -> None:
        for e in entries:
            if e.get("source") not in ("depends", "security"):
                continue
            own_scopes = list(e.get("scopes") or [])
            if e.get("security_class"):
                name = e.get("scheme_name") or type(e["dep_callable"]).__name__
                if name not in seen:
                    seen.add(name)
                    model = getattr(e["dep_callable"], "model", {}) or {}
                    out.append({
                        "scheme_name": name,
                        "scopes": list(ambient),
                        "model": model,
                    })
                continue
            sub_ambient = own_scopes if own_scopes else ambient
            walk(e.get("dep_plan", []) or [], sub_ambient)

    walk(all_entries, [])
    return out


def expand_route_level_dependencies(deps_markers):
    """Convert a `dependencies=[Depends(...), ...]` list of markers into
    synthetic plan entries that can be appended to a route's plan. Each entry
    has source=depends + dep_callable + dep_plan. The result is consumed by
    resolver before the regular handler params.

    Phase E: each marker may also be a Security() with scopes=[...]; the
    scopes are stored on the entry and propagated as parent_scopes to the
    inner SecurityScopes parameter at resolution time.
    """
    out = []
    for i, marker in enumerate(deps_markers):
        dep = getattr(marker, "dependency", None)
        if dep is None:
            continue
        cid = id(dep)
        is_security_class = bool(getattr(dep, "is_security_scheme", False))
        if is_security_class:
            sub_plan = []  # Security classes don't run a sub-plan; we call _extract.
        else:
            try:
                sub_plan = compile_route_plan(dep, "")
            except Exception:
                sub_plan = []
        import inspect as _ins
        is_async = _ins.iscoroutinefunction(dep) or (
            not _ins.isclass(dep)
            and hasattr(dep, "__call__")
            and _ins.iscoroutinefunction(dep.__call__)
        )
        scopes = list(getattr(marker, "scopes", []) or [])
        out.append({
            "name": f"_internal_dep_{i}_{cid}",
            "source": "depends",
            "type": "any",
            "default": None,
            "alias": None,
            "required": True,
            "validators": {},
            "convert_underscores": True,
            "embed": False,
            "media_type": "application/json",
            "dep_callable": dep,
            "dep_plan": sub_plan,
            "use_cache": getattr(marker, "use_cache", True) if marker is not None else True,
            "is_async": is_async,
            "_internal": True,
            "scopes": scopes,
            "security_class": is_security_class,
            "scheme_name": getattr(dep, "scheme_name", None) if is_security_class else None,
        })
    return out


def resolve_dependencies(
    plan,
    path_params,
    query_dict,
    header_lookup,
    cookie_dict,
    form_dict,
    body_bytes,
    overrides,
):
    """Walk `plan`, resolve every Depends/Security source, return:
        (kwargs: dict[str, Any], error: dict | None)

    `kwargs` contains the values to pass to the handler. If a dependency
    raises HTTPException, error is `{"status": int, "detail": Any, "headers": list}`.

    `overrides` is `app.dependency_overrides` — a dict mapping the original
    dep callable to its replacement.

    Per-request cache: keyed by `id(callable)` after override resolution.

    Phase E: when an entry's ``security_class=True``, we invoke
    ``dep_callable._extract(headers, query, cookies)`` directly. Otherwise we
    walk the sub-plan; ``security_scopes`` sub-entries are filled with the
    outer (parent) Security wrapper's scopes.
    """
    cache: dict[int, Any] = {}
    out: dict[str, Any] = {}

    def _extract_simple(entry):
        """Extract a path/query/header/cookie/body/form value for a sub-dep."""
        # Reuse the existing Rust extraction by routing it through Python —
        # we replicate the small subset of cast_scalar here to avoid round-tripping.
        name = entry["name"]
        source = entry["source"]
        type_kind = entry["type"]
        alias = entry.get("alias")
        required = entry.get("required", True)
        convert_underscores = entry.get("convert_underscores", True)
        validators = entry.get("validators", {}) or {}

        lookup_key = alias if alias else (
            name.replace("_", "-") if (source == "header" and convert_underscores) else name
        )

        # Phase L: lift cast-exception handling so a malformed value (e.g.
        # `?limit=not-a-number` for an int param) surfaces as a typed 422
        # validation error rather than a 500. Matches the top-level path used
        # by Rust dispatch — class-as-Depends sub-deps were the gap.
        loc_kind = source if source in ("path", "query", "header", "cookie", "body") else "query"
        if source in ("form", "file"):
            loc_kind = "body"

        def _safe_cast(raw_val, kind):
            try:
                return _cast(raw_val, kind, source, name, validators), None
            except (ValueError, TypeError):
                err_type = "int_parsing" if kind == "int" else (
                    "float_parsing" if kind == "float" else (
                        "bool_parsing" if kind == "bool" else (
                            "uuid_parsing" if kind == "uuid" else "type_error"
                        )
                    )
                )
                return None, {
                    "status": 422,
                    "detail": [{
                        "type": err_type,
                        "loc": [loc_kind, name],
                        "msg": f"Input should be a valid {kind}",
                        "input": raw_val,
                    }],
                }

        if source == "path":
            for n, v in path_params:
                if n == name:
                    return _safe_cast(v, type_kind)
            if required:
                return None, _missing(source, lookup_key)
            return _SENTINEL_DEFAULT, None
        if source == "query":
            vals = query_dict.get(lookup_key)
        elif source == "header":
            vals = header_lookup.get(lookup_key.lower())
        elif source == "cookie":
            v = cookie_dict.get(lookup_key)
            vals = [v] if v is not None else None
        elif source == "form" or source == "file":
            vals = form_dict.get(lookup_key)
        else:
            vals = None

        if not vals:
            if required:
                return None, _missing(source, lookup_key)
            return _SENTINEL_DEFAULT, None

        if type_kind.startswith("list["):
            inner = type_kind[5:-1]
            cast_vals = []
            for v in vals:
                cv, err = _safe_cast(v, inner)
                if err:
                    return None, err
                cast_vals.append(cv)
            return cast_vals, None
        return _safe_cast(vals[0], type_kind)

    def _resolve_dep(entry, parent_scopes=None):
        callable_ = entry["dep_callable"]
        # dependency_overrides lookup
        if overrides and callable_ in overrides:
            callable_ = overrides[callable_]
        cid = id(callable_)
        use_cache = entry.get("use_cache", True)
        if use_cache and cid in cache:
            return cache[cid], None

        # Security classes: fast-path, bypass sub-plan walk.
        if entry.get("security_class"):
            try:
                value = callable_._extract(header_lookup, query_dict, cookie_dict)
            except Exception as e:
                return None, _httpexception_dict(e)
            if use_cache:
                cache[cid] = value
            return value, None

        own_scopes = entry.get("scopes") or parent_scopes or []
        sub_kwargs: dict[str, Any] = {}
        for sub_entry in entry.get("dep_plan", []):
            if sub_entry["source"] == "security_scopes":
                from .security import SecurityScopes
                sub_kwargs[sub_entry["name"]] = SecurityScopes(scopes=list(own_scopes))
            elif sub_entry["source"] in ("depends", "security"):
                # Pass own_scopes as parent for inner security_scopes consumers.
                v, err = _resolve_dep(sub_entry, parent_scopes=own_scopes)
                if err:
                    return None, err
                sub_kwargs[sub_entry["name"]] = v
            else:
                v, err = _extract_simple(sub_entry)
                if err:
                    return None, err
                if v is not _SENTINEL_DEFAULT:
                    sub_kwargs[sub_entry["name"]] = v
        try:
            value = call_with_async_handling(callable_, sub_kwargs)
        except Exception as e:
            return None, _httpexception_dict(e)
        if use_cache:
            cache[cid] = value
        return value, None

    bg_tasks = None
    for entry in plan:
        if entry["source"] in ("depends", "security"):
            v, err = _resolve_dep(entry, parent_scopes=entry.get("scopes") or [])
            if err:
                return out, err
            # _internal entries (route/router/app-level dependencies=[]) don't
            # forward their value to the handler — they only run for side
            # effects (auth checks, etc).
            if not entry.get("_internal"):
                out[entry["name"]] = v
        elif entry["source"] == "background_tasks":
            from starlette.background import BackgroundTasks
            if bg_tasks is None:
                bg_tasks = BackgroundTasks()
                from . import _bg as _bg_state
                _bg_state._current_tasks = bg_tasks
            out[entry["name"]] = bg_tasks
        elif entry["source"] == "request":
            from . import _bg as _bg_state
            req = _bg_state._current_request
            if req is not None:
                out[entry["name"]] = req
    return out, None


_SENTINEL_DEFAULT = object()


def _cast(value, type_kind, source, name, validators):
    """Tiny replica of Rust's cast_scalar — only used for sub-dep params."""
    import re
    if type_kind == "int":
        return int(value)
    if type_kind == "float":
        return float(value)
    if type_kind == "bool":
        s = str(value).lower()
        if s in ("true", "1", "yes", "on"):
            return True
        if s in ("false", "0", "no", "off"):
            return False
        return bool(value)
    if type_kind == "uuid":
        return UUID(str(value))
    return value


def _missing(source, name):
    loc_kind = source if source in ("path", "query", "header", "cookie", "body") else "query"
    if source in ("form", "file"):
        loc_kind = "body"
    return {
        "status": 422,
        "detail": [{
            "type": "missing",
            "loc": [loc_kind, name],
            "msg": "Field required",
            "input": "",
        }],
    }


def _httpexception_dict(exc):
    """Convert an HTTPException-like exception into the error dict consumed by
    Rust dispatch. Phase F now re-raises HTTPException so the ASGI
    ExceptionMiddleware can route to a user-registered exception handler.
    Generic exceptions also propagate (becoming 500 via ServerErrorMiddleware).
    """
    raise exc


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


def _ann_is_optional(annotation: Any) -> bool:
    """True if the annotation contains a top-level Optional/Union with None,
    regardless of any wrapping ``Annotated[...]`` layers. Used to recover
    optional-ness after ``_unwrap_annotated`` has already peeled it.
    """
    import typing
    seen: set[int] = set()
    cur = annotation
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        origin = get_origin(cur)
        if origin is Annotated:
            args = get_args(cur)
            if not args:
                return False
            cur = args[0]
            continue
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
            return any(a is type(None) for a in args)
        return False
    return False


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


def _is_security_scopes_type(t: Any) -> bool:
    """True if `t` is the SecurityScopes class. We name-check first to avoid
    importing hyperfastapi.security during compile of hyperfastapi itself.
    """
    if not isinstance(t, type):
        return False
    if t.__name__ != "SecurityScopes":
        return False
    try:
        from .security import SecurityScopes
        return issubclass(t, SecurityScopes)
    except Exception:
        return False


def _is_background_tasks_type(t: Any) -> bool:
    """True if ``t`` is starlette's BackgroundTasks (which we re-export as
    ``hyperfastapi.BackgroundTasks``). Matched by class identity rather than
    name to avoid false positives.
    """
    if not isinstance(t, type):
        return False
    try:
        from starlette.background import BackgroundTasks
        return issubclass(t, BackgroundTasks)
    except Exception:
        return False


def _is_request_type(t: Any) -> bool:
    """True if ``t`` is starlette.requests.Request (or our re-export)."""
    if not isinstance(t, type):
        return False
    try:
        from starlette.requests import Request
        return issubclass(t, Request)
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
        mod = __import__("hyperfastapi._core", fromlist=[marker_name])
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


def _resolve_depends_callable(default_value: Any, marker: Any, type_hint: Any) -> Any | None:
    """Pull the dependency callable out of a Depends() marker.

    Three possible forms:
      1. `x: Annotated[T, Depends(my_func)]` — marker.dependency
      2. `x: Annotated[T, Depends()]` — class-as-dep, callable IS T (the type
         hint), e.g. `commons: Annotated[Commons, Depends()]` instantiates
         Commons(...)
      3. `x = Depends(my_func)` — default_value.dependency
    """
    for src in (marker, default_value):
        if src is None:
            continue
        dep = getattr(src, "dependency", None)
        if dep is not None:
            return dep
    # Empty Depends() with type annotation: the type IS the callable (class dep).
    if marker is not None or default_value is not None:
        return type_hint
    return None


def compile_route_plan(handler: Any, path_template: str, _seen: set[int] | None = None) -> list[dict[str, Any]]:
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

    For Depends(): we recursively compile the dependency's plan so the dispatcher
    has a flat graph to walk. Cycles are guarded by the `_seen` set.
    """
    if _seen is None:
        _seen = set()
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
    # Class-as-dep: get_type_hints(cls) returns CLASS-level annotations only
    # (typically {} for plain classes whose typing is on __init__). When the
    # handler is a class and `from __future__ import annotations` stringified
    # __init__ params, we need to resolve via __init__ explicitly.
    if not hints and inspect.isclass(handler):
        try:
            hints = get_type_hints(handler.__init__, include_extras=True)
        except Exception:
            pass
    # Callable instance: signature comes from __call__; resolve __call__'s hints.
    if not hints and not inspect.isfunction(handler) and not inspect.isclass(handler):
        call_method = getattr(handler, "__call__", None)
        if call_method is not None:
            try:
                hints = get_type_hints(call_method, include_extras=True)
            except Exception:
                pass

    for name, param in sig.parameters.items():
        ann = hints.get(name, param.annotation)
        # Detect Optional/Union[None] before _unwrap_annotated peels it.
        ann_optional = _ann_is_optional(ann)
        inner_ann, metadata = _unwrap_annotated(ann)
        # Optional -> mark required=False, peel one layer
        type_for_kind, optional = _unwrap_optional(inner_ann)
        optional = optional or ann_optional

        # Phase E: SecurityScopes parameters are filled by the resolver from
        # the wrapping Security() call; they don't extract from the request.
        if _is_security_scopes_type(type_for_kind):
            plan.append({
                "name": name,
                "source": "security_scopes",
                "type": "any",
                "default": None,
                "alias": None,
                "required": False,
                "validators": {},
                "convert_underscores": True,
                "embed": False,
                "media_type": "application/json",
            })
            continue

        # Phase F: BackgroundTasks parameter — created fresh per request and
        # stashed so the ASGI layer can drive it after the response body sends.
        if _is_background_tasks_type(type_for_kind):
            plan.append({
                "name": name,
                "source": "background_tasks",
                "type": "any",
                "default": None,
                "alias": None,
                "required": False,
                "validators": {},
                "convert_underscores": True,
                "embed": False,
                "media_type": "application/json",
            })
            continue

        # Phase I: Request parameter — handed in via the ASGI layer so handlers
        # can do `request.scope/state/url/...`.
        if _is_request_type(type_for_kind):
            plan.append({
                "name": name,
                "source": "request",
                "type": "any",
                "default": None,
                "alias": None,
                "required": False,
                "validators": {},
                "convert_underscores": True,
                "embed": False,
                "media_type": "application/json",
            })
            continue

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
                    if kind in ("Depends", "Security"):
                        # Don't unwrap — Depends/Security carry a callable,
                        # not a scalar default. The marker is consumed below.
                        break
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
            # Phase H extras: surface every marker kwarg so the OpenAPI builder
            # can read deprecated/description/title/example/examples/
            # include_in_schema without having to know each one in advance.
            "marker_kwargs": dict(marker_kwargs),
        }
        if model_class is not None and source in ("body", "form"):
            entry["model"] = model_class

        # Phase D: Depends() / Security() — resolve the dependency callable +
        # recursively compile its sub-plan. Use_cache controls per-request
        # memoization at dispatch time.
        if source in ("depends", "security"):
            dep_callable = _resolve_depends_callable(default, meta_marker, type_for_kind)
            if dep_callable is None:
                # Couldn't figure out the dependency — skip; handler default kicks in.
                continue
            # Phase E: security class instances bypass sub-plan walk; their
            # ``_extract`` reads headers/query/cookies directly.
            is_security_class = bool(getattr(dep_callable, "is_security_scheme", False))
            if is_security_class:
                entry["dep_plan"] = []
                entry["security_class"] = True
                entry["scheme_name"] = getattr(dep_callable, "scheme_name", None)
            else:
                # Detect cycles by id(callable).
                cid = id(dep_callable)
                if cid in _seen:
                    entry["dep_plan"] = []
                else:
                    _seen.add(cid)
                    try:
                        entry["dep_plan"] = compile_route_plan(dep_callable, "", _seen)
                    except Exception:
                        entry["dep_plan"] = []
                    _seen.discard(cid)
            entry["dep_callable"] = dep_callable
            # Pull use_cache from marker (defaults True). Either marker source
            # may carry it; we prefer Annotated marker over default-as-marker.
            uc = True
            scopes: list[str] = []
            for src in (meta_marker, default):
                if src is None:
                    continue
                v = getattr(src, "use_cache", None)
                if v is not None:
                    uc = bool(v)
                # Pull scopes from Security marker.
                s = getattr(src, "scopes", None)
                if s and not scopes:
                    scopes = list(s)
            entry["use_cache"] = uc
            entry["scopes"] = scopes
            # async detection
            import inspect as _ins
            entry["is_async"] = _ins.iscoroutinefunction(dep_callable) or (
                hasattr(dep_callable, "__call__")
                and _ins.iscoroutinefunction(dep_callable.__call__)
            )
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
