"""Wrapper around the bombardier load generator.

Bombardier is a Go binary; we shell out to it with `--format=json` and parse
the result. Latencies in bombardier's output are microseconds; we normalize
them to milliseconds with sane key names so consumers don't have to know
bombardier-specific units.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

THIS_DIR = Path(__file__).resolve().parent
BIN_DIR = THIS_DIR / "bin"


class BombardierNotFound(FileNotFoundError):
    """Raised when neither tests/perf/bin/ nor PATH contain a bombardier binary."""


def find_bombardier() -> Path:
    """Look for bombardier: tests/perf/bin/ → PATH. Raise with install instructions if missing."""
    candidates: list[Path] = []
    if os.name == "nt":
        candidates += [
            BIN_DIR / "bombardier.exe",
            BIN_DIR / "bombardier-windows-amd64.exe",
        ]
    candidates += [
        BIN_DIR / "bombardier",
        BIN_DIR / "bombardier-linux-amd64",
        BIN_DIR / "bombardier-darwin-amd64",
    ]

    for c in candidates:
        if c.is_file():
            return c

    on_path = shutil.which("bombardier") or (
        shutil.which("bombardier.exe") if os.name == "nt" else None
    )
    if on_path:
        return Path(on_path)

    raise BombardierNotFound(
        "bombardier binary not found. Install one of:\n"
        f"  1. Download from https://github.com/codesenberg/bombardier/releases into {BIN_DIR}\n"
        "  2. `go install github.com/codesenberg/bombardier@latest`\n"
        "  3. `choco install bombardier` (Windows, requires Chocolatey)"
    )


def run(
    target_url: str,
    *,
    concurrency: int = 100,
    duration_sec: int = 30,
    method: str = "GET",
    body: bytes | None = None,
    content_type: str | None = None,
    headers: list[tuple[str, str]] | None = None,
    timeout_sec: float | None = None,
) -> dict[str, Any]:
    """Run bombardier once and return normalized stats. Blocks until done."""
    binary = find_bombardier()

    cmd: list[str] = [
        str(binary),
        "-c", str(concurrency),
        "-d", f"{duration_sec}s",
        "-m", method,
        "-p", "r",  # print result section only
        "-o", "json",  # output format = JSON (machine-readable)
    ]

    if content_type:
        cmd += ["-H", f"Content-Type: {content_type}"]
    for k, v in headers or []:
        cmd += ["-H", f"{k}: {v}"]
    if body is not None:
        # bombardier's -b flag takes a string; we decode for that interface.
        cmd += ["-b", body.decode("utf-8", errors="replace")]

    cmd.append(target_url)

    actual_timeout = timeout_sec if timeout_sec is not None else (duration_sec + 30)

    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=actual_timeout,
    )

    if proc.returncode != 0:
        raise RuntimeError(
            f"bombardier exit {proc.returncode}\n"
            f"  command: {' '.join(cmd)}\n"
            f"  stdout: {proc.stdout[:2000]}\n"
            f"  stderr: {proc.stderr[:2000]}"
        )

    raw = json.loads(proc.stdout)
    return _normalize(raw)


def _normalize(raw: dict[str, Any]) -> dict[str, Any]:
    """Convert bombardier's nested JSON into a flatter, unit-normalized shape.

    Note: bombardier 1.2.x reports latency in microseconds and does NOT include
    latency percentiles (only mean/stddev/max). RPS does have percentiles.
    """
    result = raw.get("result", {})
    rps = result.get("rps", {})
    latency = result.get("latency", {})
    rps_pct = rps.get("percentiles", {}) or {}

    total = sum(
        result.get(k, 0) for k in ("req1xx", "req2xx", "req3xx", "req4xx", "req5xx", "others")
    )

    def _us_to_ms(x: float) -> float:
        return round(x / 1000.0, 3)

    return {
        "duration_sec": round(result.get("timeTakenSeconds", 0), 3),
        "rps_mean": round(rps.get("mean", 0), 2),
        "rps_stddev": round(rps.get("stddev", 0), 2),
        "rps_max": round(rps.get("max", 0), 2),
        "rps_p50": round(float(rps_pct.get("50", 0)), 2),
        "rps_p75": round(float(rps_pct.get("75", 0)), 2),
        "rps_p95": round(float(rps_pct.get("95", 0)), 2),
        "rps_p99": round(float(rps_pct.get("99", 0)), 2),
        "latency_ms": {
            "mean": _us_to_ms(latency.get("mean", 0)),
            "stddev": _us_to_ms(latency.get("stddev", 0)),
            "max": _us_to_ms(latency.get("max", 0)),
        },
        "requests_total": total,
        "req1xx": result.get("req1xx", 0),
        "req2xx": result.get("req2xx", 0),
        "req3xx": result.get("req3xx", 0),
        "req4xx": result.get("req4xx", 0),
        "req5xx": result.get("req5xx", 0),
        "others": result.get("others", 0),
        "errors_count": len(result.get("errors", []) or []),
        "bytes_read": result.get("bytesRead", 0),
        "bytes_written": result.get("bytesWritten", 0),
    }
