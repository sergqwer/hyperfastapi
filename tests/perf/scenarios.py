"""Perf-test scenario definitions.

Each Scenario describes one HTTP request shape that the runner will hammer at.
Adding a scenario:
1. Add the matching FastAPI route to `apps.py`.
2. Append a `Scenario(...)` here.
3. The pytest perf tests pick it up automatically through SCENARIOS parametrization.
"""

from __future__ import annotations

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
    # Below this RPS in single-process mode = regression. Conservative for now;
    # tighten after we observe stable baseline numbers.
    min_rps_single: int = 200
    min_rps_multi: int = 1000


SCENARIOS: list[Scenario] = [
    Scenario(
        name="get_plain",
        method="GET",
        path="/plain",
    ),
]


def by_name(name: str) -> Scenario:
    for s in SCENARIOS:
        if s.name == name:
            return s
    raise KeyError(
        f"No scenario named {name!r}; available: {[s.name for s in SCENARIOS]}"
    )
