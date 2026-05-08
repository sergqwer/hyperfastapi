"""Security schemes — Phase E.

All ten FastAPI-compatible security classes implemented in pure Python. Each
class is callable; the route compiler tags its plan entries with
`security_class=True` so the dispatcher invokes them via a fast-path
``_extract(headers, query, cookies)`` rather than the standard sub-plan walk.

Why a custom ``_extract`` instead of ``__call__(request)``? Our DI does not yet
ship a real Request object into Python; ``_extract`` takes the same three
lookup tables the dispatcher already builds. The public ``__call__`` matches
FastAPI's signature for source compatibility; it is only used when a user
manually invokes a security class (rare in practice).

Each instance also exposes:
    - ``scheme_name`` — key under ``components.securitySchemes`` in OpenAPI.
    - ``model``       — dict describing the scheme (``type``, ``in``, etc.).
    - ``is_security_scheme = True`` — sentinel read by ``compile_route_plan``.

`SecurityScopes` is filled at dependency-resolve time from the outer
``Security(callable, scopes=[...])`` wrapping; see ``resolve_dependencies``.

`OAuth2PasswordRequestForm` is a class-as-Depends — its ``__init__`` reads
form fields. The route compiler already handles class-as-Depends via
``__init__`` hints, so we just declare the constructor.
"""

from __future__ import annotations

import base64
import binascii
from typing import Annotated, Any

from .._core import Form
from ..exceptions import HTTPException


# ---------------------------------------------------------------------------
# Base mixin + credentials value types
# ---------------------------------------------------------------------------


class _SecurityBase:
    """Marker base. Subclasses set ``scheme_name`` and ``model`` (a dict
    matching the OpenAPI schema for that security type) plus implement
    ``_extract``.
    """

    is_security_scheme = True
    scheme_name: str = ""
    model: dict[str, Any] = {}
    auto_error: bool = True

    def __call__(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        # FastAPI compatibility shim — never used in our dispatch path.
        raise NotImplementedError(
            f"{type(self).__name__} is invoked via _extract from the route dispatcher."
        )


class HTTPBasicCredentials:
    """Username/password pair returned by HTTPBasic."""

    def __init__(self, username: str, password: str) -> None:
        self.username = username
        self.password = password


class HTTPAuthorizationCredentials:
    """``(scheme, credentials)`` pair returned by HTTPBearer / HTTPDigest."""

    def __init__(self, scheme: str, credentials: str) -> None:
        self.scheme = scheme
        self.credentials = credentials


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_authorization_scheme_param(authorization: str | None) -> tuple[str, str]:
    """Mirror FastAPI's helper: split 'Scheme param' on the first space.

    Returns ``("", "")`` when input is falsy. For a single token without a
    space ('Bearer'), returns ``(token, "")`` — *both* unset means the caller
    rejects with auth error (matching FastAPI).
    """
    if not authorization:
        return "", ""
    scheme, _, param = authorization.partition(" ")
    return scheme, param


def _first(headers: dict[str, list[str]] | dict[str, str], name: str) -> str | None:
    """Look up ``name`` (case-insensitive for headers) in a dict whose values
    are either ``list[str]`` (our internal header_lookup / query_dict shape)
    or plain ``str`` (cookies).
    """
    val = headers.get(name.lower()) if name == name.lower() else headers.get(name.lower())
    if val is None:
        # Fall back to exact key for query/cookie dicts that aren't lowercase.
        val = headers.get(name)
    if val is None:
        return None
    if isinstance(val, list):
        return val[0] if val else None
    return val


# ---------------------------------------------------------------------------
# HTTP schemes
# ---------------------------------------------------------------------------


class HTTPBasic(_SecurityBase):
    """``Authorization: Basic <base64(user:pass)>``.

    Missing or non-Basic scheme → 401 with ``WWW-Authenticate: Basic`` (so
    browsers prompt). Malformed base64 or missing colon → 401 "Invalid
    authentication credentials" — raised even if ``auto_error=False`` (this
    matches FastAPI; the contract is "graceful absence, but a present-and-
    malformed credential is always an error").
    """

    def __init__(
        self,
        *,
        scheme_name: str | None = None,
        realm: str | None = None,
        description: str | None = None,
        auto_error: bool = True,
    ) -> None:
        self.scheme_name = scheme_name or "HTTPBasic"
        self.realm = realm
        self.auto_error = auto_error
        self.model: dict[str, Any] = {"type": "http", "scheme": "basic"}
        if description is not None:
            self.model["description"] = description

    def _extract(
        self,
        headers: dict[str, list[str]],
        query: dict[str, list[str]],
        cookies: dict[str, str],
    ) -> HTTPBasicCredentials | None:
        auth_list = headers.get("authorization")
        authorization = auth_list[0] if auth_list else None
        scheme, param = _get_authorization_scheme_param(authorization)
        unauth_headers = {
            "WWW-Authenticate": f'Basic realm="{self.realm}"' if self.realm else "Basic"
        }
        if not authorization or scheme.lower() != "basic":
            if self.auto_error:
                raise HTTPException(
                    status_code=401,
                    detail="Not authenticated",
                    headers=unauth_headers,
                )
            return None
        invalid = HTTPException(
            status_code=401,
            detail="Invalid authentication credentials",
            headers=unauth_headers,
        )
        try:
            decoded = base64.b64decode(param).decode("ascii")
        except (ValueError, UnicodeDecodeError, binascii.Error):
            raise invalid
        username, separator, password = decoded.partition(":")
        if not separator:
            raise invalid
        return HTTPBasicCredentials(username=username, password=password)


class HTTPBearer(_SecurityBase):
    """``Authorization: Bearer <token>``.

    Missing creds, missing token, or wrong scheme → 403 (NOT 401 — Bearer is
    API-only, no browser prompt).
    """

    def __init__(
        self,
        *,
        bearerFormat: str | None = None,
        scheme_name: str | None = None,
        description: str | None = None,
        auto_error: bool = True,
    ) -> None:
        self.scheme_name = scheme_name or "HTTPBearer"
        self.auto_error = auto_error
        self.model: dict[str, Any] = {"type": "http", "scheme": "bearer"}
        if bearerFormat is not None:
            self.model["bearerFormat"] = bearerFormat
        if description is not None:
            self.model["description"] = description

    def _extract(
        self,
        headers: dict[str, list[str]],
        query: dict[str, list[str]],
        cookies: dict[str, str],
    ) -> HTTPAuthorizationCredentials | None:
        auth_list = headers.get("authorization")
        authorization = auth_list[0] if auth_list else None
        scheme, credentials = _get_authorization_scheme_param(authorization)
        if not (authorization and scheme and credentials):
            if self.auto_error:
                raise HTTPException(status_code=403, detail="Not authenticated")
            return None
        if scheme.lower() != "bearer":
            if self.auto_error:
                raise HTTPException(
                    status_code=403, detail="Invalid authentication credentials"
                )
            return None
        return HTTPAuthorizationCredentials(scheme=scheme, credentials=credentials)


class HTTPDigest(_SecurityBase):
    """``Authorization: Digest <params>``. Same shape as Bearer."""

    def __init__(
        self,
        *,
        scheme_name: str | None = None,
        description: str | None = None,
        auto_error: bool = True,
    ) -> None:
        self.scheme_name = scheme_name or "HTTPDigest"
        self.auto_error = auto_error
        self.model: dict[str, Any] = {"type": "http", "scheme": "digest"}
        if description is not None:
            self.model["description"] = description

    def _extract(
        self,
        headers: dict[str, list[str]],
        query: dict[str, list[str]],
        cookies: dict[str, str],
    ) -> HTTPAuthorizationCredentials | None:
        auth_list = headers.get("authorization")
        authorization = auth_list[0] if auth_list else None
        scheme, credentials = _get_authorization_scheme_param(authorization)
        if not (authorization and scheme and credentials):
            if self.auto_error:
                raise HTTPException(status_code=403, detail="Not authenticated")
            return None
        if scheme.lower() != "digest":
            if self.auto_error:
                raise HTTPException(
                    status_code=403, detail="Invalid authentication credentials"
                )
            return None
        return HTTPAuthorizationCredentials(scheme=scheme, credentials=credentials)


# ---------------------------------------------------------------------------
# API key schemes
# ---------------------------------------------------------------------------


class _APIKeyBase(_SecurityBase):
    _location: str = ""  # "header" | "query" | "cookie"

    def __init__(
        self,
        *,
        name: str,
        scheme_name: str | None = None,
        description: str | None = None,
        auto_error: bool = True,
    ) -> None:
        self.name = name
        self.scheme_name = scheme_name or type(self).__name__
        self.auto_error = auto_error
        self.model: dict[str, Any] = {
            "type": "apiKey",
            "in": self._location,
            "name": name,
        }
        if description is not None:
            self.model["description"] = description

    def _missing(self) -> None:
        if self.auto_error:
            raise HTTPException(status_code=403, detail="Not authenticated")
        return None


class APIKeyHeader(_APIKeyBase):
    _location = "header"

    def _extract(
        self,
        headers: dict[str, list[str]],
        query: dict[str, list[str]],
        cookies: dict[str, str],
    ) -> str | None:
        vals = headers.get(self.name.lower())
        api_key = vals[0] if vals else None
        if not api_key:
            return self._missing()
        return api_key


class APIKeyQuery(_APIKeyBase):
    _location = "query"

    def _extract(
        self,
        headers: dict[str, list[str]],
        query: dict[str, list[str]],
        cookies: dict[str, str],
    ) -> str | None:
        vals = query.get(self.name)
        api_key = vals[0] if vals else None
        if not api_key:
            return self._missing()
        return api_key


class APIKeyCookie(_APIKeyBase):
    _location = "cookie"

    def _extract(
        self,
        headers: dict[str, list[str]],
        query: dict[str, list[str]],
        cookies: dict[str, str],
    ) -> str | None:
        api_key = cookies.get(self.name)
        if not api_key:
            return self._missing()
        return api_key


# ---------------------------------------------------------------------------
# OAuth2 family
# ---------------------------------------------------------------------------


class OAuth2(_SecurityBase):
    """Generic OAuth2 base — used directly when a custom ``flows`` block is
    supplied; otherwise prefer the more specific subclasses below.
    """

    def __init__(
        self,
        *,
        flows: dict[str, Any] | None = None,
        scheme_name: str | None = None,
        description: str | None = None,
        auto_error: bool = True,
    ) -> None:
        self.scheme_name = scheme_name or "OAuth2"
        self.auto_error = auto_error
        self.model: dict[str, Any] = {"type": "oauth2", "flows": flows or {}}
        if description is not None:
            self.model["description"] = description

    def _extract(
        self,
        headers: dict[str, list[str]],
        query: dict[str, list[str]],
        cookies: dict[str, str],
    ) -> str | None:
        # Default: passthrough Authorization header.
        auth_list = headers.get("authorization")
        authorization = auth_list[0] if auth_list else None
        if not authorization:
            if self.auto_error:
                raise HTTPException(status_code=403, detail="Not authenticated")
            return None
        return authorization


class OAuth2PasswordBearer(_SecurityBase):
    """OAuth2 password flow — Bearer token in Authorization header.

    Returns just the token string (post-"Bearer "). Critically, an empty token
    after a literal "Bearer " is NOT rejected by the scheme — it returns ""
    so user code can run further checks. Missing or wrong-scheme → 401 with
    ``WWW-Authenticate: Bearer``.
    """

    def __init__(
        self,
        tokenUrl: str,
        *,
        scheme_name: str | None = None,
        scopes: dict[str, str] | None = None,
        description: str | None = None,
        auto_error: bool = True,
    ) -> None:
        self.scheme_name = scheme_name or "OAuth2PasswordBearer"
        self.auto_error = auto_error
        self.scopes_dict = scopes or {}
        self.model: dict[str, Any] = {
            "type": "oauth2",
            "flows": {
                "password": {
                    "tokenUrl": tokenUrl,
                    "scopes": self.scopes_dict,
                }
            },
        }
        if description is not None:
            self.model["description"] = description

    def _extract(
        self,
        headers: dict[str, list[str]],
        query: dict[str, list[str]],
        cookies: dict[str, str],
    ) -> str | None:
        auth_list = headers.get("authorization")
        authorization = auth_list[0] if auth_list else None
        scheme, param = _get_authorization_scheme_param(authorization)
        if not authorization or scheme.lower() != "bearer":
            if self.auto_error:
                raise HTTPException(
                    status_code=401,
                    detail="Not authenticated",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            return None
        return param


class OAuth2AuthorizationCodeBearer(_SecurityBase):
    """OAuth2 authorization code flow — same Bearer extraction, different OpenAPI."""

    def __init__(
        self,
        authorizationUrl: str,
        tokenUrl: str,
        *,
        refreshUrl: str | None = None,
        scheme_name: str | None = None,
        scopes: dict[str, str] | None = None,
        description: str | None = None,
        auto_error: bool = True,
    ) -> None:
        self.scheme_name = scheme_name or "OAuth2AuthorizationCodeBearer"
        self.auto_error = auto_error
        self.scopes_dict = scopes or {}
        flow: dict[str, Any] = {
            "authorizationUrl": authorizationUrl,
            "tokenUrl": tokenUrl,
            "scopes": self.scopes_dict,
        }
        if refreshUrl is not None:
            flow["refreshUrl"] = refreshUrl
        self.model: dict[str, Any] = {
            "type": "oauth2",
            "flows": {"authorizationCode": flow},
        }
        if description is not None:
            self.model["description"] = description

    def _extract(
        self,
        headers: dict[str, list[str]],
        query: dict[str, list[str]],
        cookies: dict[str, str],
    ) -> str | None:
        auth_list = headers.get("authorization")
        authorization = auth_list[0] if auth_list else None
        scheme, param = _get_authorization_scheme_param(authorization)
        if not authorization or scheme.lower() != "bearer":
            if self.auto_error:
                raise HTTPException(
                    status_code=401,
                    detail="Not authenticated",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            return None
        return param


class OpenIdConnect(_SecurityBase):
    """OIDC — passes through the entire Authorization header (including scheme)."""

    def __init__(
        self,
        *,
        openIdConnectUrl: str,
        scheme_name: str | None = None,
        description: str | None = None,
        auto_error: bool = True,
    ) -> None:
        self.scheme_name = scheme_name or "OpenIdConnect"
        self.auto_error = auto_error
        self.model: dict[str, Any] = {
            "type": "openIdConnect",
            "openIdConnectUrl": openIdConnectUrl,
        }
        if description is not None:
            self.model["description"] = description

    def _extract(
        self,
        headers: dict[str, list[str]],
        query: dict[str, list[str]],
        cookies: dict[str, str],
    ) -> str | None:
        auth_list = headers.get("authorization")
        authorization = auth_list[0] if auth_list else None
        if not authorization:
            if self.auto_error:
                raise HTTPException(status_code=403, detail="Not authenticated")
            return None
        return authorization  # full header value, by FastAPI design


# ---------------------------------------------------------------------------
# OAuth2 form-data dependency
# ---------------------------------------------------------------------------


class OAuth2PasswordRequestForm:
    """Standard OAuth2 password-grant body parser.

    Used as ``Depends(OAuth2PasswordRequestForm)``. The route compiler walks
    ``__init__``'s signature to wire form-field extraction. ``username`` and
    ``password`` have no defaults so a missing field surfaces as 422 via the
    standard "Field required" path.
    """

    def __init__(
        self,
        *,
        grant_type: Annotated[str | None, Form(pattern="^password$")] = None,
        username: Annotated[str, Form()],
        password: Annotated[str, Form()],
        scope: Annotated[str, Form()] = "",
        client_id: Annotated[str | None, Form()] = None,
        client_secret: Annotated[str | None, Form()] = None,
    ) -> None:
        self.grant_type = grant_type
        self.username = username
        self.password = password
        self.scopes = scope.split() if scope else []
        self.client_id = client_id
        self.client_secret = client_secret


# ---------------------------------------------------------------------------
# SecurityScopes — receives the cumulative scopes from outer Security() calls
# ---------------------------------------------------------------------------


class SecurityScopes:
    """Receiver for ``Security(callable, scopes=[...])`` scope lists.

    Filled by ``resolve_dependencies`` based on the wrapping Security entry's
    scopes. Empty if the dependency was reached without a Security wrapper.
    """

    def __init__(self, scopes: list[str] | None = None) -> None:
        self.scopes = scopes or []
        self.scope_str = " ".join(self.scopes)


__all__ = [
    "OAuth2",
    "OAuth2AuthorizationCodeBearer",
    "OAuth2PasswordBearer",
    "OAuth2PasswordRequestForm",
    "HTTPBasic",
    "HTTPBasicCredentials",
    "HTTPBearer",
    "HTTPDigest",
    "HTTPAuthorizationCredentials",
    "APIKeyHeader",
    "APIKeyQuery",
    "APIKeyCookie",
    "OpenIdConnect",
    "SecurityScopes",
]
