"""Run every Scenario against both backends — uvicorn (vanilla FastAPI vs the
Rust port) and hyper (Rust port only) — then dump a JSON suitable for the
chart-rendering script in docs/perf/.

Usage:
    HYPERFASTAPI_AS_FASTAPI=1 python tests/perf/compare_backends.py --duration 5

Output: docs/perf/results.json with the schema:
    {
      "system": {...},
      "duration_sec": 5,
      "scenarios": [
        {"name": ..., "fastapi_uvicorn_single": ..., "fastapi_uvicorn_multi": ...,
         "hyperfastapi_uvicorn_single": ..., "hyperfastapi_uvicorn_multi": ...,
         "hyperfastapi_hyper_single": ..., "hyperfastapi_hyper_multi": ...},
        ...
      ]
    }
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import time
from typing import Any

THIS_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR.parent))

from perf.runner import run_single, run_multi  # noqa: E402
from perf.scenarios import SCENARIOS  # noqa: E402
from perf import system_info  # noqa: E402


def _set_env(use_rust: bool) -> None:
    if use_rust:
        os.environ["HYPERFASTAPI_AS_FASTAPI"] = "1"
    else:
        os.environ.pop("HYPERFASTAPI_AS_FASTAPI", None)


def _bench(scenario, mode: str, duration: int, backend: str, use_rust: bool) -> dict[str, Any]:
    """Run one scenario in either single or multi mode against the chosen backend.
    Subprocess server inherits env, so HYPERFASTAPI_AS_FASTAPI must already be set.
    """
    _set_env(use_rust)
    if mode == "single":
        return run_single(scenario, duration_sec=duration, backend=backend)
    return run_multi(scenario, duration_sec=duration, backend=backend)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=int, default=5,
                        help="seconds per scenario (per backend × mode)")
    parser.add_argument("--out", type=pathlib.Path,
                        default=THIS_DIR.parent.parent / "docs" / "perf" / "results.json")
    parser.add_argument("--scenarios", nargs="*", default=None,
                        help="subset of scenario names; default: all")
    args = parser.parse_args()

    selected = SCENARIOS
    if args.scenarios:
        chosen = set(args.scenarios)
        selected = [s for s in SCENARIOS if s.name in chosen]

    out_rows: list[dict[str, Any]] = []
    for s in selected:
        print(f"\n=== {s.name} ===")
        row: dict[str, Any] = {"name": s.name}

        # 1. Vanilla FastAPI on uvicorn (single + multi)
        for mode in ("single", "multi"):
            r = _bench(s, mode, args.duration, "uvicorn", use_rust=False)
            row[f"fastapi_uvicorn_{mode}"] = r["rps_mean"]
            print(f"  fastapi+uvicorn  {mode:6} : {r['rps_mean']:>9.0f} RPS  "
                  f"5xx={r['req5xx']}")

        # 2. hyperfastapi on uvicorn (single + multi)
        for mode in ("single", "multi"):
            r = _bench(s, mode, args.duration, "uvicorn", use_rust=True)
            row[f"hyperfastapi_uvicorn_{mode}"] = r["rps_mean"]
            print(f"  hyperfastapi+uv  {mode:6} : {r['rps_mean']:>9.0f} RPS  "
                  f"5xx={r['req5xx']}")

        # 3. hyperfastapi on hyper (single + multi)
        for mode in ("single", "multi"):
            r = _bench(s, mode, args.duration, "hyper", use_rust=True)
            row[f"hyperfastapi_hyper_{mode}"] = r["rps_mean"]
            print(f"  hyperfastapi+hyp {mode:6} : {r['rps_mean']:>9.0f} RPS  "
                  f"5xx={r['req5xx']}")

        out_rows.append(row)

    output = {
        "run_id": time.strftime("%Y-%m-%dT%H-%M-%SZ", time.gmtime()),
        "duration_sec": args.duration,
        "system": system_info.collect(),
        "scenarios": out_rows,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n  -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
