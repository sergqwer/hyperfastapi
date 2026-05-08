"""FastAPI app used by perf benchmarks.

Launched out-of-process by `runner.py`:
    uvicorn apps:app --app-dir <abs>/tests/perf --workers N --port 8001 --no-access-log

Endpoints are kept minimal in Phase 1 — only the scenarios actually exercised
by `scenarios.SCENARIOS` need to exist. New scenarios add new routes here.
"""

from __future__ import annotations

from fastapi import FastAPI

app = FastAPI(
    title="fastapi-rust-tests perf bench",
    # Docs/openapi off — they add surface that pollutes routing benchmarks.
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@app.get("/health")
def health() -> dict:
    """Readiness probe — the runner polls this until 200 before starting bombardier."""
    return {"status": "ok"}


@app.get("/plain")
def plain() -> dict:
    """Baseline scenario: zero params, zero deps, smallest possible JSON body."""
    return {"ok": True}
