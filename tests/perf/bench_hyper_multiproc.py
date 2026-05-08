"""Spawn N separate Python processes each running the hyper server on its
own port, then hammer all of them in parallel and report aggregate RPS.

This is the multi-process scaling story for hyperfastapi: each Python proc
has its own GIL, so 4 procs ≈ 4× single-proc throughput.

Usage:
    HYPERFASTAPI_AS_FASTAPI=1 python tests/perf/bench_hyper_multiproc.py \
        --workers 4 --duration 5 --scenarios get_plain get_with_query
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import pathlib
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

THIS_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR.parent))

from perf import bombardier  # noqa: E402
from perf.scenarios import SCENARIOS  # noqa: E402


def _free_ports(n: int) -> list[int]:
    ports: list[int] = []
    socks: list[socket.socket] = []
    try:
        for _ in range(n):
            s = socket.socket()
            s.bind(("127.0.0.1", 0))
            ports.append(s.getsockname()[1])
            socks.append(s)
        return ports
    finally:
        for s in socks:
            s.close()


@contextlib.contextmanager
def _hyper_servers(n_procs: int):
    ports = _free_ports(n_procs)
    procs: list[subprocess.Popen] = []
    try:
        for p in ports:
            cmd = [sys.executable, str(THIS_DIR / "native_launcher.py"),
                   "--port", str(p), "--workers", "1"]
            procs.append(subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                cwd=str(THIS_DIR), env=os.environ.copy(),
            ))
        # Wait for all servers ready.
        import httpx
        for p in ports:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                try:
                    if httpx.get(f"http://127.0.0.1:{p}/health", timeout=1).status_code == 200:
                        break
                except Exception:
                    time.sleep(0.1)
            else:
                raise TimeoutError(f"server on :{p} not ready")
        yield ports
    finally:
        for proc in procs:
            try:
                import psutil
                psutil.Process(proc.pid).terminate()
            except Exception:
                proc.terminate()


def _run_one_bombardier(port: int, scenario, duration: int, conc_per_proc: int) -> dict:
    return bombardier.run(
        target_url=f"http://127.0.0.1:{port}{scenario.path}",
        concurrency=conc_per_proc,
        duration_sec=duration,
        method=scenario.method,
        body=scenario.body,
        content_type=scenario.content_type,
        headers=list(scenario.headers),
    )


def _bench_aggregate(scenario, ports: list[int], duration: int) -> dict:
    """Hammer every port in parallel; sum the RPS."""
    conc = scenario.concurrency  # per-proc
    with ThreadPoolExecutor(max_workers=len(ports)) as ex:
        futs = [ex.submit(_run_one_bombardier, p, scenario, duration, conc)
                for p in ports]
        results = [f.result() for f in futs]
    return {
        "rps_total": sum(r["rps_mean"] for r in results),
        "rps_per_proc": [r["rps_mean"] for r in results],
        "errors_5xx_total": sum(r["req5xx"] for r in results),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=os.cpu_count() or 4,
                        help="how many separate Python procs to spawn")
    parser.add_argument("--duration", type=int, default=5)
    parser.add_argument("--scenarios", nargs="*", default=None)
    parser.add_argument("--out", type=pathlib.Path,
                        default=THIS_DIR.parent.parent / "docs" / "perf" / "multiproc.json")
    args = parser.parse_args()

    selected = SCENARIOS
    if args.scenarios:
        chosen = set(args.scenarios)
        selected = [s for s in SCENARIOS if s.name in chosen]

    rows: list[dict] = []
    with _hyper_servers(args.workers) as ports:
        time.sleep(2)
        for s in selected:
            print(f"\n=== {s.name} (n_procs={args.workers}) ===")
            r = _bench_aggregate(s, ports, args.duration)
            rps_total = r["rps_total"]
            print(f"  aggregate: {rps_total:>9.0f} RPS  "
                  f"(per-proc: {[round(x) for x in r['rps_per_proc']]})  "
                  f"5xx={r['errors_5xx_total']}")
            rows.append({
                "name": s.name,
                "workers": args.workers,
                "rps_total": rps_total,
                "rps_per_proc": r["rps_per_proc"],
                "errors_5xx_total": r["errors_5xx_total"],
            })

    output = {
        "run_id": time.strftime("%Y-%m-%dT%H-%M-%SZ", time.gmtime()),
        "duration_sec": args.duration,
        "workers": args.workers,
        "scenarios": rows,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n  -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
