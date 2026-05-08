"""FastAPI app used by perf benchmarks.

Launched out-of-process by `runner.py`:
    uvicorn apps:app --app-dir <abs>/tests/perf --workers N --port 8001 --no-access-log

Each route here corresponds to one entry in `scenarios.SCENARIOS`. Routes are
intentionally minimal — they exercise one specific code path so we can attribute
RPS deltas between Python FastAPI and the Rust port to the right component.
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Annotated

# uvicorn loads this module in a fresh subprocess — the parent's sys.modules
# alias from tests/conftest.py doesn't survive the spawn. Re-apply it here so
# perf bench actually exercises the Rust port when the env var is set.
if os.environ.get("FASTAPI_RUST_AS_FASTAPI") == "1":
    import fastapi_rust  # noqa: F401
    import fastapi_rust.exceptions
    import fastapi_rust.params
    import fastapi_rust.responses
    import fastapi_rust.security
    import fastapi_rust.encoders
    import fastapi_rust.testclient
    import fastapi_rust.staticfiles
    import fastapi_rust.templating
    import fastapi_rust.middleware
    import fastapi_rust.middleware.cors
    import fastapi_rust.middleware.gzip
    import fastapi_rust.middleware.trustedhost

    sys.modules["fastapi"] = fastapi_rust
    sys.modules["fastapi.exceptions"] = fastapi_rust.exceptions
    sys.modules["fastapi.params"] = fastapi_rust.params
    sys.modules["fastapi.responses"] = fastapi_rust.responses
    sys.modules["fastapi.security"] = fastapi_rust.security
    sys.modules["fastapi.encoders"] = fastapi_rust.encoders
    sys.modules["fastapi.testclient"] = fastapi_rust.testclient
    sys.modules["fastapi.staticfiles"] = fastapi_rust.staticfiles
    sys.modules["fastapi.templating"] = fastapi_rust.templating
    sys.modules["fastapi.middleware"] = fastapi_rust.middleware
    sys.modules["fastapi.middleware.cors"] = fastapi_rust.middleware.cors
    sys.modules["fastapi.middleware.gzip"] = fastapi_rust.middleware.gzip
    sys.modules["fastapi.middleware.trustedhost"] = fastapi_rust.middleware.trustedhost

from fastapi import Depends, FastAPI, Header, Path, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from pydantic import BaseModel

app = FastAPI(
    title="fastapi-rust-tests perf bench",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

# Three middleware in the stack for the with-middleware scenario; they run on
# every request, but only `/with-middleware` is the perf target. Other endpoints
# also pay the middleware cost — that's intentional, so the comparison reflects
# real production deployments where middleware is global.
app.add_middleware(GZipMiddleware, minimum_size=1024)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------- Models ----------


class Item(BaseModel):
    name: str
    price: float
    qty: int = 1


class ItemOut(BaseModel):
    name: str
    price: float


class LargePayload(BaseModel):
    title: str
    items: list[Item]
    metadata: dict[str, str]


# ---------- Dependencies ----------


def common_dep() -> dict:
    return {"injected": True}


def step_a() -> int:
    return 1


def step_b(a: Annotated[int, Depends(step_a)]) -> int:
    return a + 1


def step_c(b: Annotated[int, Depends(step_b)]) -> int:
    return b + 1


# ---------- Routes ----------


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/plain")
def plain() -> dict:
    return {"ok": True}


@app.get("/with-query")
def with_query(q: str, limit: int = 10) -> dict:
    return {"q": q, "limit": limit}


@app.get("/with-path/{item_id}")
def with_path(item_id: int) -> dict:
    return {"item_id": item_id}


@app.get("/with-headers")
def with_headers(x_token: Annotated[str, Header()]) -> dict:
    return {"x_token": x_token}


@app.post("/post-validated")
def post_validated(item: Item) -> dict:
    return item.model_dump()


@app.post("/post-large-body")
def post_large_body(payload: LargePayload) -> dict:
    return {"items": len(payload.items), "title": payload.title}


@app.get("/with-dep")
def with_dep(dep: Annotated[dict, Depends(common_dep)]) -> dict:
    return dep


@app.get("/with-chain")
def with_chain(c: Annotated[int, Depends(step_c)]) -> dict:
    return {"c": c}


@app.get("/response-model", response_model=ItemOut)
def response_model() -> dict:
    # Returns extra fields that response_model must filter out
    return {"name": "Foo", "price": 9.99, "internal": "secret", "extra": [1, 2, 3]}


@app.get("/async")
async def async_handler() -> dict:
    return {"async": True}


@app.get("/async-io")
async def async_io_handler() -> dict:
    # Yield to the event loop once — exercises the async runtime hot path
    await asyncio.sleep(0)
    return {"yielded": True}


@app.get("/with-middleware")
def with_middleware() -> dict:
    """Same as /plain, but explicit so scenarios.py can target it.
    Middleware is GLOBAL, so /plain pays the same cost — but having a named
    route makes the scenario self-documenting in results.json.
    """
    return {"ok": True}
