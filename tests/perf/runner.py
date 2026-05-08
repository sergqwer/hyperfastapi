"""Orchestrate uvicorn server + bombardier benchmark.

Two entry points:
  1. CLI: `cd tests && python -m perf.runner --scenario get_plain --mode both --duration 10`
  2. From pytest: import `run_single` / `run_multi` and call directly.

Modes:
- single: 1 uvicorn worker pinned to one core (CPU affinity, single-thread under GIL)
- multi:  N workers (N = os.cpu_count()), no affinity, full machine
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import httpx

from . import bombardier, system_info
from .scenarios import SCENARIOS, Scenario, by_name

THIS_DIR = Path(__file__).resolve().parent
TESTS_DIR = THIS_DIR.parent
RESULTS_DIR = TESTS_DIR / "results" / "perf"


@dataclass
class RunConfig:
    scenario: Scenario
    mode: str  # "single_process" | "process_per_core"
    workers: int
    duration_sec: int
    concurrency: int
    pinned_core: int | None = None


def _free_port() -> int:
    """Pick a free TCP port on 127.0.0.1 to avoid collisions in parallel CI runs."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _wait_for_ready(url: str, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            r = httpx.get(url, timeout=1.0)
            if r.status_code == 200:
                return
        except Exception as e:
            last_exc = e
        time.sleep(0.1)
    raise TimeoutError(
        f"Server at {url} not ready within {timeout}s; last error: {last_exc!r}"
    )


def _build_uvicorn_cmd(workers: int, port: int) -> list[str]:
    return [
        sys.executable,
        "-m",
        "uvicorn",
        "apps:app",
        "--app-dir",
        str(THIS_DIR),
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--workers",
        str(workers),
        "--no-access-log",
        "--log-level",
        "warning",
    ]


def _terminate_tree(proc: subprocess.Popen) -> None:
    """Kill uvicorn supervisor AND all its workers (Windows leaves zombies otherwise)."""
    if proc.poll() is not None:
        return
    try:
        import psutil

        parent = psutil.Process(proc.pid)
        children = parent.children(recursive=True)
        for c in children:
            with contextlib.suppress(psutil.NoSuchProcess):
                c.terminate()
        parent.terminate()
        gone, alive = psutil.wait_procs([parent, *children], timeout=5)
        for p in alive:
            with contextlib.suppress(psutil.NoSuchProcess):
                p.kill()
    except Exception:
        # Fallback if psutil chokes for any reason
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


@contextlib.contextmanager
def _server(workers: int, port: int, pinned_core: int | None) -> Iterator[subprocess.Popen]:
    cmd = _build_uvicorn_cmd(workers=workers, port=port)
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        cwd=str(THIS_DIR),
    )

    # Affinity only makes sense for single-worker mode (otherwise children inherit
    # the same mask and crowd onto one core, defeating the multi-process test).
    if pinned_core is not None and workers == 1:
        try:
            import psutil

            psutil.Process(proc.pid).cpu_affinity([pinned_core])
        except Exception as e:
            print(f"[runner] Warning: could not set CPU affinity: {e}", file=sys.stderr)

    try:
        _wait_for_ready(f"http://127.0.0.1:{port}/health")
        yield proc
    finally:
        _terminate_tree(proc)


def _bench(config: RunConfig, port: int) -> dict[str, Any]:
    target_url = f"http://127.0.0.1:{port}{config.scenario.path}"

    # 2-second warmup so JIT/lazy paths settle before we measure.
    time.sleep(2)

    stats = bombardier.run(
        target_url=target_url,
        concurrency=config.concurrency,
        duration_sec=config.duration_sec,
        method=config.scenario.method,
        body=config.scenario.body,
        content_type=config.scenario.content_type,
        headers=list(config.scenario.headers),
    )

    return {
        "name": config.scenario.name,
        "mode": config.mode,
        "workers": config.workers,
        "pinned_core": config.pinned_core,
        "concurrency": config.concurrency,
        "duration_sec": config.duration_sec,
        **stats,
    }


def run_single(scenario: Scenario, duration_sec: int = 30) -> dict[str, Any]:
    """1 worker pinned to core 0 — exposes the GIL-bound single-thread ceiling."""
    port = _free_port()
    config = RunConfig(
        scenario=scenario,
        mode="single_process",
        workers=1,
        duration_sec=duration_sec,
        concurrency=scenario.concurrency,
        pinned_core=0,
    )
    with _server(workers=1, port=port, pinned_core=0):
        return _bench(config, port)


def run_multi(scenario: Scenario, duration_sec: int = 30) -> dict[str, Any]:
    """N workers across all cores — production-style baseline for FastAPI."""
    workers = os.cpu_count() or 1
    port = _free_port()
    # Scale concurrency with workers so each worker sees similar load to the single case.
    concurrency = scenario.concurrency * workers
    config = RunConfig(
        scenario=scenario,
        mode="process_per_core",
        workers=workers,
        duration_sec=duration_sec,
        concurrency=concurrency,
        pinned_core=None,
    )
    with _server(workers=workers, port=port, pinned_core=None):
        return _bench(config, port)


def write_results(rows: list[dict[str, Any]], run_id: str | None = None) -> Path:
    if run_id is None:
        run_id = time.strftime("%Y-%m-%dT%H-%M-%SZ", time.gmtime())
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RESULTS_DIR / f"{run_id}.json"

    try:
        import fastapi

        framework_version = getattr(fastapi, "__version__", "unknown")
    except Exception:
        framework_version = "unknown"

    output = {
        "run_id": run_id,
        "framework": "fastapi",
        "framework_version": framework_version,
        "system": system_info.collect(),
        "load_gen": {"tool": "bombardier"},
        "scenarios": rows,
    }

    out_path.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
    return out_path


def main() -> int:
    p = argparse.ArgumentParser(description="Run a perf scenario via uvicorn + bombardier")
    p.add_argument(
        "--scenario",
        required=True,
        help=f"Scenario name. Available: {[s.name for s in SCENARIOS]}",
    )
    p.add_argument(
        "--mode",
        choices=["single", "multi", "both"],
        default="both",
        help="single = 1 worker pinned to core 0; multi = N workers across cores",
    )
    p.add_argument("--duration", type=int, default=30, help="Bench duration in seconds")
    args = p.parse_args()

    scenario = by_name(args.scenario)
    rows: list[dict[str, Any]] = []

    if args.mode in ("single", "both"):
        print(
            f"[runner] single_process: scenario={scenario.name} duration={args.duration}s"
        )
        row = run_single(scenario, args.duration)
        rows.append(row)
        print(
            f"  -> rps_mean={row['rps_mean']:.0f}  "
            f"latency_max={row['latency_ms']['max']:.2f}ms  "
            f"5xx={row['req5xx']}"
        )

    if args.mode in ("multi", "both"):
        print(
            f"[runner] process_per_core: scenario={scenario.name} "
            f"workers={os.cpu_count()} duration={args.duration}s"
        )
        row = run_multi(scenario, args.duration)
        rows.append(row)
        print(
            f"  -> rps_mean={row['rps_mean']:.0f}  "
            f"latency_max={row['latency_ms']['max']:.2f}ms  "
            f"5xx={row['req5xx']}"
        )

    out_path = write_results(rows)
    print(f"[runner] results -> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
