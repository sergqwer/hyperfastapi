"""Perf-test scenario definitions.

Each Scenario describes one HTTP request shape that the runner will hammer at.
Adding a scenario:
1. Add the matching FastAPI route to `apps.py`.
2. Append a `Scenario(...)` here.
3. The pytest perf tests pick it up automatically through SCENARIOS parametrization.
"""

from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass(frozen=True)
class Scenario:
    name: str
    method: str = "GET"
    path: str = "/"
    body: bytes | None = None
    content_type: str | None = None
    # Tuple of (key, value) so the dataclass remains hashable/frozen.
    headers: tuple[tuple[str, str], ...] = ()
    expected_status: int = 200
    # Load profile defaults — can be overridden from CLI or test parametrize.
    concurrency: int = 100
    duration_sec: int = 30
    # Below this RPS = regression. Conservative for now; tighten as baseline stabilizes.
    min_rps_single: int = 200
    min_rps_multi: int = 1000


# Body for the post_validated scenario — small Pydantic-model JSON.
_VALIDATED_BODY = json.dumps({"name": "perf", "price": 1.99, "qty": 1}).encode()


# Body for the post_large_body scenario — ~50KB payload exercising big-body parsing.
def _make_large_body() -> bytes:
    items = [
        {"name": f"item-{i:04d}", "price": float(i), "qty": i % 10}
        for i in range(500)
    ]
    payload = {
        "title": "Large Payload",
        "items": items,
        "metadata": {f"key-{i}": f"value-{i}" * 5 for i in range(20)},
    }
    return json.dumps(payload).encode()


_LARGE_BODY = _make_large_body()


SCENARIOS: list[Scenario] = [
    Scenario(
        name="get_plain",
        method="GET",
        path="/plain",
    ),
    Scenario(
        name="get_with_query",
        method="GET",
        path="/with-query?q=foo&limit=20",
    ),
    Scenario(
        name="get_with_path",
        method="GET",
        path="/with-path/42",
    ),
    Scenario(
        name="get_with_headers",
        method="GET",
        path="/with-headers",
        headers=(("X-Token", "abc123"),),
    ),
    Scenario(
        name="post_validated",
        method="POST",
        path="/post-validated",
        body=_VALIDATED_BODY,
        content_type="application/json",
    ),
    Scenario(
        name="post_large_body",
        method="POST",
        path="/post-large-body",
        body=_LARGE_BODY,
        content_type="application/json",
        # Large-body bench is slower per request; lower thresholds.
        min_rps_single=50,
        min_rps_multi=200,
    ),
    Scenario(
        name="get_with_dep",
        method="GET",
        path="/with-dep",
    ),
    Scenario(
        name="get_with_chain_deps",
        method="GET",
        path="/with-chain",
    ),
    Scenario(
        name="get_response_model",
        method="GET",
        path="/response-model",
    ),
    Scenario(
        name="get_async",
        method="GET",
        path="/async",
    ),
    Scenario(
        name="get_async_io",
        method="GET",
        path="/async-io",
    ),
    Scenario(
        name="get_with_middleware",
        method="GET",
        path="/with-middleware",
    ),
]


def by_name(name: str) -> Scenario:
    for s in SCENARIOS:
        if s.name == name:
            return s
    raise KeyError(
        f"No scenario named {name!r}; available: {[s.name for s in SCENARIOS]}"
    )
