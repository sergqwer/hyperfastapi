"""Perf-specific pytest hooks.

Collects per-test perf rows in a session-level list and writes one results
JSON file at the end of the session — so `pytest tests/perf` produces a
single comparable artifact regardless of how many scenarios were run.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Put tests/ on sys.path so we can `from perf.runner import ...`. Critical:
# we must NOT put tests/perf/ on sys.path — that would import runner.py as a
# top-level `runner` module without parent package, breaking its `from .` imports.
THIS_DIR = Path(__file__).resolve().parent  # tests/perf
TESTS_DIR = THIS_DIR.parent  # tests
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

import pytest  # noqa: E402  (must come after sys.path tweak)

_perf_rows: list[dict] = []


@pytest.fixture
def perf_recorder() -> list[dict]:
    """Tests append their result row to this list; the session hook flushes to disk."""
    return _perf_rows


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if not _perf_rows:
        return
    from perf.runner import write_results

    out_path = write_results(_perf_rows)
    print(f"\n[perf] results -> {out_path}")
