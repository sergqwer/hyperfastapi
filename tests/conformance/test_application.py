"""FastAPI() application object — constructor params, lifespan, debug mode.

Each test uses a fresh `FastAPI()` instance so they don't bleed state.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_constructor_no_args_creates_valid_app() -> None:
    app = FastAPI()
    client = TestClient(app)
    # Default app has no routes, but /openapi.json is auto-mounted.
    response = client.get("/openapi.json")
    assert response.status_code == 200, response.text
    schema = response.json()
    assert schema["info"]["title"] == "FastAPI"
    assert schema["info"]["version"] == "0.1.0"
    assert schema["openapi"].startswith("3.")


def test_title_and_version_reflected_in_openapi() -> None:
    app = FastAPI(title="My Service", version="2.5.1")
    client = TestClient(app)
    schema = client.get("/openapi.json").json()
    assert schema["info"]["title"] == "My Service"
    assert schema["info"]["version"] == "2.5.1"


def test_openapi_url_none_disables_openapi_and_docs() -> None:
    app = FastAPI(openapi_url=None)
    client = TestClient(app)
    assert client.get("/openapi.json").status_code == 404
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404


def test_docs_url_none_disables_swagger_only() -> None:
    """Disabling /docs alone should not affect /openapi.json or /redoc."""
    app = FastAPI(docs_url=None)
    client = TestClient(app)
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 200
    assert client.get("/redoc").status_code == 200


def test_redoc_url_none_disables_redoc_only() -> None:
    app = FastAPI(redoc_url=None)
    client = TestClient(app)
    assert client.get("/redoc").status_code == 404
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").status_code == 200


def test_description_appears_in_openapi() -> None:
    app = FastAPI(description="A test service")
    client = TestClient(app)
    schema = client.get("/openapi.json").json()
    assert schema["info"]["description"] == "A test service"


def test_root_path_in_openapi_servers() -> None:
    """When the app sits behind a reverse proxy, root_path informs OpenAPI servers."""
    app = FastAPI(root_path="/api/v1")
    client = TestClient(app)
    schema = client.get("/openapi.json").json()
    servers = schema.get("servers", [])
    # FastAPI 0.x adds a server entry with the root_path
    assert any(s.get("url") == "/api/v1" for s in servers), (
        f"Expected '/api/v1' in servers, got {servers}"
    )


def test_lifespan_runs_startup_and_shutdown() -> None:
    """`lifespan` must invoke the asynccontextmanager body once on startup and once on shutdown."""
    events: list[str] = []

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        events.append("startup")
        yield
        events.append("shutdown")

    app = FastAPI(lifespan=lifespan)

    @app.get("/ping")
    def ping() -> dict:
        return {"ok": True}

    # TestClient as a context manager triggers lifespan
    with TestClient(app) as client:
        assert events == ["startup"]
        response = client.get("/ping")
        assert response.status_code == 200

    assert events == ["startup", "shutdown"]


def test_multiple_apps_do_not_share_routes() -> None:
    """Two FastAPI() instances are independent — a route on one is not on the other."""
    app_a = FastAPI()
    app_b = FastAPI()

    @app_a.get("/from-a")
    def from_a() -> dict:
        return {"app": "a"}

    client_a = TestClient(app_a)
    client_b = TestClient(app_b)

    assert client_a.get("/from-a").status_code == 200
    assert client_b.get("/from-a").status_code == 404


def test_default_response_class_jsonresponse() -> None:
    """Without explicit `default_response_class`, a dict-returning route is served as JSON."""
    app = FastAPI()

    @app.get("/json")
    def get_json() -> dict:
        return {"x": 1}

    response = TestClient(app).get("/json")
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {"x": 1}


@pytest.mark.parametrize(
    "param,value,info_key",
    [
        ("title", "MyApp", "title"),
        ("version", "9.9.9", "version"),
        ("description", "Hello", "description"),
        ("summary", "Short summary", "summary"),
        ("terms_of_service", "https://example.com/terms", "termsOfService"),
    ],
)
def test_simple_info_fields_reach_openapi(param: str, value: str, info_key: str) -> None:
    app = FastAPI(**{param: value})
    schema = TestClient(app).get("/openapi.json").json()
    assert schema["info"][info_key] == value
