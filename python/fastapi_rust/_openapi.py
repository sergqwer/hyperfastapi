"""Phase H — OpenAPI 3.1 schema builder.

Walks the route summaries produced by ``FastAPI._routes_summary`` and emits a
schema that mirrors FastAPI's output closely enough that the conformance tests
pass: parameters block per route, requestBody for body params, responses with
content/schema, components.schemas with referenced Pydantic models, plus the
auto-generated ``HTTPValidationError`` and ``ValidationError`` schemas FastAPI
attaches to any 422-capable route.

Byte-exact match with FastAPI is **not** the goal — we ship the same shape with
matching keys. Phase J revisits if a snapshot diff demands tighter ordering.
"""

from __future__ import annotations

from typing import Any


# ---------------------------------------------------------------------------
# Validation-error schemas — FastAPI hard-codes these in
# fastapi.openapi.utils. Replicated so 422 responses have something to
# `$ref`.
# ---------------------------------------------------------------------------

VALIDATION_ERROR_SCHEMA: dict[str, Any] = {
    "title": "ValidationError",
    "type": "object",
    "properties": {
        "loc": {
            "title": "Location",
            "type": "array",
            "items": {"anyOf": [{"type": "string"}, {"type": "integer"}]},
        },
        "msg": {"title": "Message", "type": "string"},
        "type": {"title": "Error Type", "type": "string"},
    },
    "required": ["loc", "msg", "type"],
}

HTTP_VALIDATION_ERROR_SCHEMA: dict[str, Any] = {
    "title": "HTTPValidationError",
    "type": "object",
    "properties": {
        "detail": {
            "title": "Detail",
            "type": "array",
            "items": {"$ref": "#/components/schemas/ValidationError"},
        }
    },
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _validator_to_schema(validators: dict[str, Any]) -> dict[str, Any]:
    """Convert plan validator constraints to OpenAPI 3.1 schema fragments."""
    out: dict[str, Any] = {}
    for k, v in validators.items():
        if k == "gt":
            out["exclusiveMinimum"] = v
        elif k == "ge":
            out["minimum"] = v
        elif k == "lt":
            out["exclusiveMaximum"] = v
        elif k == "le":
            out["maximum"] = v
        elif k == "min_length":
            out["minLength"] = v
        elif k == "max_length":
            out["maxLength"] = v
        elif k == "pattern":
            out["pattern"] = v
    return out


_TYPE_KIND_TO_OAS = {
    "int": {"type": "integer"},
    "float": {"type": "number"},
    "str": {"type": "string"},
    "bool": {"type": "boolean"},
    "uuid": {"type": "string", "format": "uuid"},
    "list[int]": {"type": "array", "items": {"type": "integer"}},
    "list[float]": {"type": "array", "items": {"type": "number"}},
    "list[str]": {"type": "array", "items": {"type": "string"}},
    "any": {},
    "raw": {"type": "object"},
}


def _type_kind_to_schema(type_kind: str) -> dict[str, Any]:
    return dict(_TYPE_KIND_TO_OAS.get(type_kind, {}))


def _pydantic_schema(model_class: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return ``(top_level_schema, defs)`` for a Pydantic model. ``defs`` are
    the entries to merge into ``components.schemas``. The top-level schema's
    ``title`` matches the class name so a ``$ref`` works.
    """
    try:
        full = model_class.model_json_schema(ref_template="#/components/schemas/{model}")
    except Exception:
        return {"type": "object"}, {}
    defs = full.pop("$defs", {}) or {}
    # Strip nullable container we don't want on the top-level model.
    return full, defs


def _is_validation_capable(route: dict[str, Any]) -> bool:
    """A route can produce 422 if any param has a non-trivial source."""
    for p in route.get("param_plan", []) or []:
        src = p.get("source")
        if src in ("path", "query", "header", "cookie", "body", "form", "file"):
            return True
        if src in ("depends", "security"):
            # Conservatively yes — the dep may itself raise validation errors.
            return True
    return False


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def build_openapi_schema(app: Any) -> dict[str, Any]:
    """Construct the OpenAPI 3.1 dict served at ``/openapi.json``."""
    routes = app._routes_summary()
    schema: dict[str, Any] = {"openapi": "3.1.0"}

    info: dict[str, Any] = {"title": app.title, "version": app.version}
    desc = app.description
    if desc:
        info["description"] = desc
    summary = getattr(app, "summary", None)
    if summary:
        info["summary"] = summary
    tos = getattr(app, "terms_of_service", None)
    if tos:
        info["termsOfService"] = tos
    schema["info"] = info

    # When the app is mounted behind a reverse proxy via ``root_path``, OpenAPI
    # callers need a ``servers`` entry so generated client URLs include the
    # prefix.
    root_path = getattr(app, "root_path", "") or ""
    if root_path:
        schema["servers"] = [{"url": root_path}]

    paths: dict[str, Any] = {}
    components_schemas: dict[str, Any] = {}
    security_schemes: dict[str, Any] = {}
    any_validation_capable = False

    for r in routes:
        if not r.get("include_in_schema", True):
            continue
        path = r["path"]
        method = r["method"].lower()
        path_item = paths.setdefault(path, {})
        op: dict[str, Any] = {}
        if r.get("tags"):
            op["tags"] = list(r["tags"])
        if r.get("summary"):
            op["summary"] = r["summary"]
        if r.get("description"):
            op["description"] = r["description"]
        if r.get("operation_id"):
            op["operationId"] = r["operation_id"]
        if r.get("deprecated"):
            op["deprecated"] = True

        # ---- parameters: path / query / header / cookie ----
        parameters: list[dict[str, Any]] = []
        body_entries: list[dict[str, Any]] = []
        for p in r.get("param_plan", []) or []:
            src = p.get("source")
            if src in ("path", "query", "header", "cookie"):
                param_dict = _param_to_openapi(p, src)
                if param_dict is not None:
                    parameters.append(param_dict)
            elif src == "body":
                body_entries.append(p)
            elif src == "form" or src == "file":
                body_entries.append(p)
        if parameters:
            op["parameters"] = parameters

        # ---- requestBody ----
        if body_entries:
            req_body, body_defs = _build_request_body(body_entries)
            if req_body:
                op["requestBody"] = req_body
            for k, v in body_defs.items():
                components_schemas.setdefault(k, v)

        # ---- responses ----
        is_capable = _is_validation_capable(r)
        any_validation_capable = any_validation_capable or is_capable
        responses, resp_defs = _build_responses(r, is_capable)
        op["responses"] = responses
        for k, v in resp_defs.items():
            components_schemas.setdefault(k, v)

        # ---- security per-route + global schemes ----
        if r.get("security"):
            sec_array: list[dict[str, list[str]]] = []
            for s in r["security"]:
                sec_array.append({s["scheme_name"]: list(s.get("scopes") or [])})
                # First-seen wins for the global components entry.
                security_schemes.setdefault(s["scheme_name"], s["model"])
            op["security"] = sec_array

        path_item[method] = op

    # ---- HTTPValidationError + ValidationError if any 422 may surface ----
    if any_validation_capable:
        components_schemas.setdefault("HTTPValidationError", HTTP_VALIDATION_ERROR_SCHEMA)
        components_schemas.setdefault("ValidationError", VALIDATION_ERROR_SCHEMA)

    schema["paths"] = paths
    components: dict[str, Any] = {"schemas": components_schemas}
    if security_schemes:
        components["securitySchemes"] = security_schemes
    schema["components"] = components

    return schema


def _param_to_openapi(p: dict[str, Any], src: str) -> dict[str, Any] | None:
    """Build an OpenAPI 3.x parameter object for a path/query/header/cookie.

    Returns ``None`` when ``include_in_schema=False`` was set on the marker —
    callers filter these out of the parameters array.
    """
    mkw = p.get("marker_kwargs") or {}
    if mkw.get("include_in_schema") is False:
        return None
    name = p.get("alias") or p["name"]
    if src == "header":
        if not p.get("alias") and p.get("convert_underscores", True):
            name = p["name"].replace("_", "-")
    type_kind = p.get("type", "any")
    schema_part = _type_kind_to_schema(type_kind)
    schema_part.update(_validator_to_schema(p.get("validators") or {}))
    if p.get("default") is not None and not p.get("required", True):
        schema_part["default"] = p["default"]
    if "title" in mkw and mkw["title"] is not None:
        schema_part["title"] = mkw["title"]
    else:
        schema_part.setdefault("title", p["name"].replace("_", " ").title())
    out: dict[str, Any] = {
        "name": name,
        "in": src,
        "required": bool(p.get("required", True)),
        "schema": schema_part,
    }
    if mkw.get("description"):
        out["description"] = mkw["description"]
    if mkw.get("deprecated"):
        out["deprecated"] = True
    if mkw.get("example") is not None:
        out["example"] = mkw["example"]
    if mkw.get("openapi_examples"):
        out["examples"] = mkw["openapi_examples"]
    return out


def _build_request_body(body_entries: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Build the OpenAPI ``requestBody`` block. Single Pydantic body becomes a
    `$ref`; form/file becomes a multipart schema; multiple body params become
    an inline object schema with each name a property.
    """
    defs: dict[str, Any] = {}
    if not body_entries:
        return None, defs

    # Single body Pydantic model
    if len(body_entries) == 1 and body_entries[0].get("source") == "body":
        e = body_entries[0]
        model = e.get("model")
        if model is not None:
            top, sub_defs = _pydantic_schema(model)
            name = top.get("title") or model.__name__
            defs[name] = top
            defs.update(sub_defs)
            return {
                "required": bool(e.get("required", True)),
                "content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{name}"}}},
            }, defs
        # raw dict body
        return {
            "required": bool(e.get("required", True)),
            "content": {"application/json": {"schema": {"type": "object"}}},
        }, defs

    # Form/file body
    if all(e.get("source") in ("form", "file") for e in body_entries):
        properties: dict[str, Any] = {}
        required_list: list[str] = []
        is_multipart = any(e.get("source") == "file" for e in body_entries)
        for e in body_entries:
            tk = e.get("type", "str")
            properties[e["name"]] = _type_kind_to_schema(
                tk if not tk.startswith("uploadfile") else "str"
            )
            if e.get("required", True):
                required_list.append(e["name"])
        body_schema: dict[str, Any] = {"type": "object", "properties": properties}
        if required_list:
            body_schema["required"] = required_list
        ct = "multipart/form-data" if is_multipart else "application/x-www-form-urlencoded"
        return {"required": True, "content": {ct: {"schema": body_schema}}}, defs

    # Multi-body or mixed: inline object schema per name.
    properties = {}
    required_list = []
    for e in body_entries:
        if e.get("source") != "body":
            continue
        model = e.get("model")
        if model is not None:
            top, sub_defs = _pydantic_schema(model)
            cname = top.get("title") or model.__name__
            defs[cname] = top
            defs.update(sub_defs)
            properties[e["name"]] = {"$ref": f"#/components/schemas/{cname}"}
        else:
            properties[e["name"]] = _type_kind_to_schema(e.get("type", "any"))
        if e.get("required", True):
            required_list.append(e["name"])
    body_schema = {"type": "object", "properties": properties}
    if required_list:
        body_schema["required"] = required_list
    return {"required": True, "content": {"application/json": {"schema": body_schema}}}, defs


def _build_responses(route: dict[str, Any], capable: bool) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build the responses map for a route. status_code (default 200) gets the
    success entry; if validation can occur, 422 is added with HTTPValidationError.
    Additional ``responses={code: {model, description}}`` entries from the
    decorator are merged.
    """
    defs: dict[str, Any] = {}
    code = route.get("status_code") or 200
    rdesc = route.get("response_description") or "Successful Response"
    success: dict[str, Any] = {"description": rdesc}
    no_body = code in (204, 304) or code < 200 or (code >= 300 and code < 400)
    if not no_body:
        rm = route.get("response_model")
        if rm is not None:
            top, sub_defs = _pydantic_schema(rm)
            cname = top.get("title") or getattr(rm, "__name__", "Response")
            defs[cname] = top
            defs.update(sub_defs)
            success["content"] = {
                "application/json": {"schema": {"$ref": f"#/components/schemas/{cname}"}}
            }
        else:
            success["content"] = {"application/json": {"schema": {}}}
    responses: dict[str, Any] = {str(code): success}

    # Additional ``responses=`` from the decorator: each value can have
    # ``model`` (becomes a $ref + content/application_json) and ``description``.
    extra = route.get("responses") or {}
    for status_code, entry in extra.items():
        if not isinstance(entry, dict):
            continue
        out_resp: dict[str, Any] = {}
        if entry.get("description"):
            out_resp["description"] = entry["description"]
        else:
            out_resp["description"] = ""
        model = entry.get("model")
        if model is not None:
            try:
                top, sub_defs = _pydantic_schema(model)
                cname = top.get("title") or getattr(model, "__name__", "Response")
                defs[cname] = top
                defs.update(sub_defs)
                out_resp["content"] = {
                    "application/json": {"schema": {"$ref": f"#/components/schemas/{cname}"}}
                }
            except Exception:
                pass
        responses[str(status_code)] = out_resp

    if capable and "422" not in responses:
        responses["422"] = {
            "description": "Validation Error",
            "content": {
                "application/json": {
                    "schema": {"$ref": "#/components/schemas/HTTPValidationError"}
                }
            },
        }
    return responses, defs
