"""WebSocket throughput benchmark.

Measures echo round-trip rate (msg/sec) on a single WebSocket connection.
Compares hyperfastapi.run_native() vs FastAPI+uvicorn so users can see the
relative gain.

Why single-connection: asyncio.gather() across multiple WS clients on
Windows shows a known scheduling pathology (issue #N) that caps throughput
at ~30 msg/s regardless of backend; we test single-connection until the
multi-connection cooperative-scheduling fix lands.

Usage:
    HYPERFASTAPI_AS_FASTAPI=1 python tests/perf/ws_bench.py
    HYPERFASTAPI_AS_FASTAPI=1 python tests/perf/ws_bench.py --messages 5000
    HYPERFASTAPI_AS_FASTAPI=1 python tests/perf/ws_bench.py --backend hyper
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import pathlib
import socket
import statistics
import subprocess
import sys
import time

import httpx
import websockets

THIS_DIR = pathlib.Path(__file__).resolve().parent


def _free_port() -> int:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _start_server(backend: str, port: int) -> subprocess.Popen:
    env = os.environ.copy()
    env["HYPERFASTAPI_AS_FASTAPI"] = "1"
    if backend == "hyper":
        cmd = [sys.executable, str(THIS_DIR / "ws_launcher.py"),
               "--port", str(port), "--workers", "4"]
    elif backend == "uvicorn":
        cmd = [sys.executable, "-m", "uvicorn", "ws_app:app",
               "--host", "127.0.0.1", "--port", str(port),
               "--workers", "1", "--no-access-log", "--log-level", "warning",
               "--app-dir", str(THIS_DIR)]
    else:
        raise ValueError(f"unknown backend {backend!r}")

    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, cwd=str(THIS_DIR), env=env)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"http://127.0.0.1:{port}/health", timeout=1)
            if r.status_code == 200:
                return proc
        except Exception:
            time.sleep(0.1)
    proc.terminate()
    raise TimeoutError(f"{backend} server on :{port} not ready")


@contextlib.contextmanager
def _server(backend: str, port: int):
    proc = _start_server(backend, port)
    try:
        yield
    finally:
        try:
            import psutil
            parent = psutil.Process(proc.pid)
            for c in parent.children(recursive=True):
                try:
                    c.terminate()
                except Exception:
                    pass
            parent.terminate()
        except Exception:
            proc.terminate()


async def _echo_roundtrip(uri: str, n_msgs: int, payload: str) -> dict:
    """Open one WS connection, fire n_msgs echo round-trips sequentially.
    Sequential (not asyncio.gather) — see module docstring."""
    samples: list[float] = []
    async with websockets.connect(uri, max_size=2**20, ping_interval=None,
                                  close_timeout=1.0) as ws:
        # Warm-up — one message before we start the timer so the server's
        # first-handler latency doesn't skew the small samples.
        await ws.send(payload)
        await ws.recv()

        start = time.perf_counter()
        for i in range(n_msgs):
            t0 = time.perf_counter()
            await ws.send(payload)
            await ws.recv()
            if i % max(1, n_msgs // 100) == 0:
                samples.append((time.perf_counter() - t0) * 1000.0)
        elapsed = time.perf_counter() - start

    samples.sort()
    return {
        "total_msgs": n_msgs,
        "elapsed_sec": elapsed,
        "throughput_msg_s": n_msgs / elapsed if elapsed else 0.0,
        "latency_p50_ms": statistics.median(samples) if samples else 0.0,
        "latency_p99_ms": samples[int(len(samples) * 0.99)] if len(samples) >= 100 else (samples[-1] if samples else 0.0),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--messages", type=int, default=2000,
                        help="messages per connection (sequential round-trips)")
    parser.add_argument("--payload-size", type=int, default=64,
                        help="message payload size in bytes")
    parser.add_argument("--backend", default="both",
                        choices=["hyper", "uvicorn", "both"])
    args = parser.parse_args()

    backends = ["hyper", "uvicorn"] if args.backend == "both" else [args.backend]
    payload = "x" * args.payload_size

    print("=" * 70)
    print(f"WebSocket echo round-trip bench  msgs={args.messages}  "
          f"payload={args.payload_size}B  (single-connection sequential)")
    print("=" * 70)

    results: dict[str, dict] = {}
    for backend in backends:
        port = _free_port()
        with _server(backend, port):
            uri = f"ws://127.0.0.1:{port}/echo"
            res = asyncio.run(_echo_roundtrip(uri, args.messages, payload))
            results[backend] = res
            print(f"\n  backend={backend}  port={port}")
            print(f"    elapsed     = {res['elapsed_sec']:.3f} s")
            print(f"    throughput  = {res['throughput_msg_s']:>10,.0f} msg/sec")
            print(f"    latency p50 = {res['latency_p50_ms']:.3f} ms")
            print(f"    latency p99 = {res['latency_p99_ms']:.3f} ms")
            print(f"    total msgs  = {res['total_msgs']:,}")

    if "hyper" in results and "uvicorn" in results:
        ratio = results["hyper"]["throughput_msg_s"] / results["uvicorn"]["throughput_msg_s"]
        print(f"\n  hyperfastapi vs uvicorn: {ratio:.2f}x throughput")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
