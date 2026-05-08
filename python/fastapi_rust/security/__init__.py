"""Security schemes — Phase A: minimal stub classes that satisfy the import
contract. Phase E replaces these with Rust-backed implementations using the
SIMD base64 codec for HTTPBasic.
"""

from __future__ import annotations

from typing import Any


class _SecuritySchemeStub:
    """Common stub base — Phase E replaces with proper PyO3 PyClasses that
    integrate with the Rust dispatch pipeline.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._args = args
        self._kwargs = kwargs
        self.scheme_name = type(self).__name__
        self.auto_error = kwargs.get("auto_error", True)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return None


class OAuth2PasswordBearer(_SecuritySchemeStub):
    pass


class OAuth2PasswordRequestForm:
    """Form dependency — Phase E implements full multipart parsing path."""

    def __init__(
        self,
        username: str = "",
        password: str = "",
        grant_type: str | None = None,
        scope: str = "",
        client_id: str | None = None,
        client_secret: str | None = None,
    ) -> None:
        self.username = username
        self.password = password
        self.grant_type = grant_type
        self.scopes = scope.split() if scope else []
        self.client_id = client_id
        self.client_secret = client_secret


class HTTPBasic(_SecuritySchemeStub):
    pass


class HTTPBasicCredentials:
    def __init__(self, username: str, password: str) -> None:
        self.username = username
        self.password = password


class HTTPBearer(_SecuritySchemeStub):
    pass


class HTTPDigest(_SecuritySchemeStub):
    pass


class HTTPAuthorizationCredentials:
    def __init__(self, scheme: str, credentials: str) -> None:
        self.scheme = scheme
        self.credentials = credentials


class APIKeyHeader(_SecuritySchemeStub):
    pass


class APIKeyQuery(_SecuritySchemeStub):
    pass


class APIKeyCookie(_SecuritySchemeStub):
    pass


class OpenIdConnect(_SecuritySchemeStub):
    pass


class OAuth2(_SecuritySchemeStub):
    pass


class OAuth2AuthorizationCodeBearer(_SecuritySchemeStub):
    pass


class SecurityScopes:
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
