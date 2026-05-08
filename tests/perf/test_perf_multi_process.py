"""Multi-process perf tests: N uvicorn workers (N = os.cpu_count()), no affinity.

This is the production-style FastAPI deployment baseline. Expect roughly
linear scaling vs. single-process for I/O-bound or simple JSON endpoints —
the ratio (multi_rps / single_rps) is the GIL-efficiency factor that the
Rust port aims to achieve in a single process.
"""

from __future__ import annotations

import pytest

from .runner import run_multi
from .scenarios import SCENARIOS

pytestmark = [pytest.mark.perf_multi, pytest.mark.slow]


@pytest.mark.parametrize(
    "scenario", SCENARIOS, ids=[s.name for s in SCENARIOS]
)
def test_perf_multi_process(scenario, perf_recorder) -> None:
    row = run_multi(scenario, duration_sec=10)
    perf_recorder.append(row)

    print(
        f"\n[multi {scenario.name} workers={row['workers']}] "
        f"rps={row['rps_mean']:.0f}  "
        f"max_latency={row['latency_ms']['max']:.2f}ms  "
        f"4xx={row['req4xx']}  5xx={row['req5xx']}"
    )

    assert row["req5xx"] == 0, (
        f"server errors during bench: {row['req5xx']} responses with 5xx status"
    )
    assert row["rps_mean"] >= scenario.min_rps_multi, (
        f"RPS regression in process-per-core mode: "
        f"{row['rps_mean']:.0f} < {scenario.min_rps_multi} (scenario={scenario.name})"
    )
