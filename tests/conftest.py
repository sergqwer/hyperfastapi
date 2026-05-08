"""Pytest configuration for the FastAPI conformance + perf test suite.

Captures per-test outcomes via pytest_runtest_makereport and writes a
machine-readable JSON summary to tests/results/conformance/<timestamp>.json
at session end. Perf tests write their own results from perf/runner.py.

Set `HYPERFASTAPI_AS_FASTAPI=1` to run the same tests against the Rust port:
  - sys.modules["fastapi"] (and submodules) are aliased to the hyperfastapi
    package, so `from fastapi import ...` resolves to our Rust extension
    without any code changes in the test files.
  - Without that env var, the suite runs against vanilla fastapi as usual.
"""

from __future__ import annotations

import json
import os
import platform
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

# ----- Optional hyperfastapi alias (must run before pytest collects tests) -
# Done at module import time, BEFORE pytest does anything else with imports,
# so test files that do `from fastapi import FastAPI` get our Rust class.
if os.environ.get("HYPERFASTAPI_AS_FASTAPI") == "1":
    import hyperfastapi  # noqa: F401  — triggers the cdylib load
    import hyperfastapi.exceptions
    import hyperfastapi.params
    import hyperfastapi.responses
    import hyperfastapi.security
    import hyperfastapi.encoders
    import hyperfastapi.testclient
    import hyperfastapi.staticfiles
    import hyperfastapi.templating
    import hyperfastapi.middleware
    import hyperfastapi.middleware.cors
    import hyperfastapi.middleware.gzip
    import hyperfastapi.middleware.trustedhost

    # Top-level + submodules must all be aliased so `from fastapi.<sub> import X`
    # finds the right object.
    sys.modules["fastapi"] = hyperfastapi
    sys.modules["fastapi.exceptions"] = hyperfastapi.exceptions
    sys.modules["fastapi.params"] = hyperfastapi.params
    sys.modules["fastapi.responses"] = hyperfastapi.responses
    sys.modules["fastapi.security"] = hyperfastapi.security
    sys.modules["fastapi.encoders"] = hyperfastapi.encoders
    sys.modules["fastapi.testclient"] = hyperfastapi.testclient
    sys.modules["fastapi.staticfiles"] = hyperfastapi.staticfiles
    sys.modules["fastapi.templating"] = hyperfastapi.templating
    sys.modules["fastapi.middleware"] = hyperfastapi.middleware
    sys.modules["fastapi.middleware.cors"] = hyperfastapi.middleware.cors
    sys.modules["fastapi.middleware.gzip"] = hyperfastapi.middleware.gzip
    sys.modules["fastapi.middleware.trustedhost"] = hyperfastapi.middleware.trustedhost

import pytest

TESTS_DIR = Path(__file__).resolve().parent
RESULTS_DIR = TESTS_DIR / "results"

_run_started_at: float = 0.0
_run_id: str = ""
_outcomes: dict[str, str] = {}
_failures: list[dict[str, Any]] = []


def _category_for(nodeid: str) -> str:
    """Map a pytest nodeid to a category like 'request_params/query' or 'security/oauth2'.

    Pytest nodeids use forward slashes regardless of OS:
        'conformance/request_params/test_query.py::test_x' → 'request_params/query'
        'D:/.../tests/conformance/test_imports.py::test_y' → 'imports'
    """
    path_part = nodeid.split("::")[0]
    parts = path_part.replace("\\", "/").split("/")
    if "conformance" not in parts:
        return "other"
    idx = parts.index("conformance")
    rest = parts[idx + 1 :]
    if not rest:
        return "uncategorized"
    rest[-1] = rest[-1].removeprefix("test_").removesuffix(".py")
    return "/".join(rest)


def _framework_name() -> str:
    """Reports `hyperfastapi` when the alias is active, else `fastapi`."""
    return "hyperfastapi" if os.environ.get("HYPERFASTAPI_AS_FASTAPI") == "1" else "fastapi"


def _fastapi_version() -> str:
    try:
        import fastapi

        return getattr(fastapi, "__version__", "unknown")
    except Exception:
        return "unavailable"


def pytest_configure(config: pytest.Config) -> None:
    global _run_started_at, _run_id
    _run_started_at = time.time()
    _run_id = time.strftime("%Y-%m-%dT%H-%M-%SZ", time.gmtime(_run_started_at))


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[Any]):
    outcome = yield
    report = outcome.get_result()
    if report.when != "call":
        return
    _outcomes[item.nodeid] = report.outcome
    if report.outcome == "failed":
        _failures.append(
            {
                "nodeid": item.nodeid,
                "category": _category_for(item.nodeid),
                "longrepr": str(report.longrepr)[:2000] if report.longrepr else "",
            }
        )


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    has_conformance = any("conformance" in nid for nid in _outcomes)
    if not has_conformance:
        return

    by_category: dict[str, dict[str, int]] = defaultdict(
        lambda: {"total": 0, "passed": 0, "failed": 0, "skipped": 0}
    )
    for nodeid, outcome in _outcomes.items():
        if "conformance" not in nodeid:
            continue
        cat = _category_for(nodeid)
        by_category[cat]["total"] += 1
        if outcome in ("passed", "failed", "skipped"):
            by_category[cat][outcome] += 1

    conf = [o for nid, o in _outcomes.items() if "conformance" in nid]
    summary = {
        "total": len(conf),
        "passed": sum(1 for o in conf if o == "passed"),
        "failed": sum(1 for o in conf if o == "failed"),
        "skipped": sum(1 for o in conf if o == "skipped"),
        "duration_sec": round(time.time() - _run_started_at, 2),
    }

    output = {
        "run_id": _run_id,
        "framework": _framework_name(),
        "framework_version": _fastapi_version(),
        "python_version": sys.version.split()[0],
        "platform": platform.platform(),
        "summary": summary,
        "by_category": dict(sorted(by_category.items())),
        "failures": _failures,
    }

    out_dir = RESULTS_DIR / "conformance"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{_run_id}.json"
    out_path.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n[conformance] results -> {out_path}")


@pytest.fixture
def repo_root() -> Path:
    return TESTS_DIR.parent


@pytest.fixture
def tests_dir() -> Path:
    return TESTS_DIR


@pytest.fixture
def results_dir() -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    return RESULTS_DIR
