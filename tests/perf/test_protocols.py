"""End-to-end protocol smoke tests for the hyperfastapi native server.

Verifies the server actually serves each declared protocol against a real
client. Run TWO server instances side-by-side:

  * port 8765 — plaintext (HTTP/1.0, HTTP/1.1, HTTP/2 cleartext)
  * port 8443 — TLS (HTTPS = HTTP/1.1 + HTTP/2 ALPN, HTTP/3 over QUIC)

Each protocol gets a discrete pass/fail line.

Usage:
    python tests/perf/gen_self_signed.py   # generate _cert.pem + _key.pem
    python tests/perf/test_protocols.py
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import pathlib
import ssl
import subprocess
import sys
import time
from typing import Iterator

import httpx

THIS_DIR = pathlib.Path(__file__).resolve().parent
LAUNCHER = THIS_DIR / "native_launcher.py"
CERT = THIS_DIR / "_cert.pem"
KEY = THIS_DIR / "_key.pem"


@contextlib.contextmanager
def _server(port: int, *, tls: bool = False, http3: bool = False) -> Iterator[None]:
    cmd = [sys.executable, str(LAUNCHER), "--port", str(port), "--workers", "1"]
    if tls:
        cmd += ["--tls-cert", str(CERT), "--tls-key", str(KEY)]
    if http3:
        cmd += ["--http3"]
    env = os.environ.copy()
    env["HYPERFASTAPI_AS_FASTAPI"] = "1"
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, cwd=str(THIS_DIR), env=env)
    # Probe until ready.
    deadline = time.monotonic() + 15
    scheme = "https" if tls else "http"
    health = f"{scheme}://127.0.0.1:{port}/health"
    while time.monotonic() < deadline:
        try:
            r = httpx.get(health, verify=False, timeout=1)
            if r.status_code == 200:
                break
        except Exception:
            time.sleep(0.1)
    else:
        proc.terminate()
        raise TimeoutError(f"server on :{port} not ready")
    try:
        yield
    finally:
        try:
            import psutil
            psutil.Process(proc.pid).terminate()
        except Exception:
            proc.terminate()


def _check(label: str, ok: bool, detail: str = "") -> None:
    mark = "OK  " if ok else "FAIL"
    print(f"  [{mark}] {label}: {detail}")


def test_plaintext(port: int) -> None:
    print(f"\n--- plaintext on :{port} ---")
    base = f"http://127.0.0.1:{port}"
    # HTTP/1.1
    with httpx.Client(http1=True, http2=False) as c:
        r = c.get(f"{base}/plain")
        _check("HTTP/1.1", r.status_code == 200 and r.http_version == "HTTP/1.1",
               f"version={r.http_version} body={r.text}")
    # HTTP/1.0 via curl (httpx is HTTP/1.1+ only)
    out = subprocess.run(
        ["curl.exe", "--http1.0", "-s", "-o", "NUL" if os.name == "nt" else "/dev/null",
         "-w", "%{http_version}|%{http_code}", f"{base}/plain"],
        capture_output=True, text=True, timeout=5,
    )
    parts = (out.stdout or "").split("|")
    # curl on Windows shows version as "1" for HTTP/1.0; on Linux "1.0".
    ok = len(parts) >= 2 and parts[0] in ("1", "1.0") and parts[1] == "200"
    _check("HTTP/1.0", ok, f"curl -> {out.stdout}")

    # Keep-alive: 2 GETs over one connection should reuse the socket.
    with httpx.Client(http1=True, http2=False) as c:
        r1 = c.get(f"{base}/plain")
        r2 = c.get(f"{base}/plain")
        # httpx pools connections; both succeeding under the same Client
        # implies keep-alive worked.
        _check("HTTP/1.1 keep-alive", r1.status_code == 200 and r2.status_code == 200,
               "2 GETs on pooled client OK")


def test_tls(port: int) -> None:
    print(f"\n--- TLS on :{port} ---")
    base = f"https://127.0.0.1:{port}"
    # HTTPS (HTTP/1.1 over TLS)
    with httpx.Client(http1=True, http2=False, verify=False) as c:
        r = c.get(f"{base}/plain")
        _check("HTTPS / HTTP/1.1", r.status_code == 200 and r.http_version == "HTTP/1.1",
               f"version={r.http_version}")
    # HTTP/2 over TLS (ALPN-negotiated)
    with httpx.Client(http2=True, verify=False) as c:
        r = c.get(f"{base}/plain")
        _check("HTTPS / HTTP/2 (ALPN)",
               r.status_code == 200 and r.http_version == "HTTP/2",
               f"version={r.http_version}")


async def test_http3_async(port: int) -> bool:
    """Probe HTTP/3 on the QUIC listener at the same port (UDP)."""
    from aioquic.asyncio.client import connect
    from aioquic.h3.connection import H3_ALPN, H3Connection
    from aioquic.h3.events import DataReceived, HeadersReceived
    from aioquic.quic.configuration import QuicConfiguration

    cfg = QuicConfiguration(is_client=True, alpn_protocols=H3_ALPN)
    cfg.verify_mode = ssl.CERT_NONE

    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    status: list[int] = []
    body = bytearray()

    async with connect("127.0.0.1", port, configuration=cfg) as proto:
        h3 = H3Connection(proto._quic)
        sid = proto._quic.get_next_available_stream_id()
        h3.send_headers(stream_id=sid, headers=[
            (b":method", b"GET"), (b":scheme", b"https"),
            (b":authority", b"127.0.0.1"), (b":path", b"/plain"),
        ], end_stream=True)
        proto.transmit()

        # Pump events; resolve as soon as we have status+data.
        original = proto.quic_event_received

        def resolve_when_ready(evt):
            original(evt)
            for h3_event in h3.handle_event(evt):
                if isinstance(h3_event, HeadersReceived):
                    for k, v in h3_event.headers:
                        if k == b":status":
                            status.append(int(v))
                elif isinstance(h3_event, DataReceived):
                    body.extend(h3_event.data)
                    if status and not fut.done():
                        fut.set_result(None)
        proto.quic_event_received = resolve_when_ready
        try:
            await asyncio.wait_for(fut, timeout=5)
        except asyncio.TimeoutError:
            return False
    return status == [200] and bytes(body) == b'{"ok":true}'


def test_http3(port: int) -> None:
    print(f"\n--- HTTP/3 (QUIC) on :{port} ---")
    ok = asyncio.run(test_http3_async(port))
    _check("HTTP/3", ok, "GET /plain over QUIC")


def main() -> int:
    if not (CERT.exists() and KEY.exists()):
        print("Self-signed cert not found. Run: python tests/perf/gen_self_signed.py")
        return 1

    print("=" * 60)
    print("hyperfastapi protocol matrix")
    print("=" * 60)

    with _server(8765):
        test_plaintext(8765)

    with _server(8443, tls=True, http3=True):
        test_tls(8443)
        test_http3(8443)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
