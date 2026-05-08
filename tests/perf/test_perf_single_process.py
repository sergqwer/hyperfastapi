"""Single-process perf tests: 1 uvicorn worker, pinned to core 0.

This is the GIL-bound ceiling for FastAPI. The Rust port should beat these
numbers by 1-2 orders of magnitude on the same hardware in single-process mode.
"""

from __future__ import annotations

import pytest

from .runner import run_single
from .scenarios import SCENARIOS

pytestmark = [pytest.mark.perf_single, pytest.mark.slow]


@pytest.mark.parametrize(
    "scenario", SCENARIOS, ids=[s.name for s in SCENARIOS]
)
def test_perf_single_process(scenario, perf_recorder) -> None:
    row = run_single(scenario, duration_sec=10)
    perf_recorder.append(row)

    print(
        f"\n[single {scenario.name}] "
        f"rps={row['rps_mean']:.0f}  "
        f"max_latency={row['latency_ms']['max']:.2f}ms  "
        f"4xx={row['req4xx']}  5xx={row['req5xx']}"
    )

    assert row["req5xx"] == 0, (
        f"server errors during bench: {row['req5xx']} responses with 5xx status"
    )
    assert row["rps_mean"] >= scenario.min_rps_single, (
        f"RPS regression in single-process mode: "
        f"{row['rps_mean']:.0f} < {scenario.min_rps_single} (scenario={scenario.name})"
    )
