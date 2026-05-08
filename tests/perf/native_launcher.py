"""Phase N: subprocess entry point for hyper-served perf benchmarks.

Replaces ``uvicorn apps:app ...`` invocation in runner.py with a Python
script that imports apps:app and calls ``app.run_native(host, port, workers)``.
The Rust hyper server takes over from there — no asyncio loop, no ASGI scope
construction, no middleware stack.
"""

from __future__ import annotations

import argparse
import os
import sys


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--workers", type=int, default=0)
    args = parser.parse_args()

    # Same alias trick as tests/conftest.py + tests/perf/apps.py — must run
    # before the import below so `from fastapi import FastAPI` resolves to
    # fastapi_rust when FASTAPI_RUST_AS_FASTAPI=1.
    if os.environ.get("FASTAPI_RUST_AS_FASTAPI") == "1":
        import fastapi_rust  # noqa: F401
        import fastapi_rust.exceptions
        import fastapi_rust.params
        import fastapi_rust.responses
        import fastapi_rust.security
        import fastapi_rust.encoders
        import fastapi_rust.testclient
        import fastapi_rust.staticfiles
        import fastapi_rust.templating
        import fastapi_rust.middleware
        import fastapi_rust.middleware.cors
        import fastapi_rust.middleware.gzip
        import fastapi_rust.middleware.trustedhost

        sys.modules["fastapi"] = fastapi_rust
        sys.modules["fastapi.exceptions"] = fastapi_rust.exceptions
        sys.modules["fastapi.params"] = fastapi_rust.params
        sys.modules["fastapi.responses"] = fastapi_rust.responses
        sys.modules["fastapi.security"] = fastapi_rust.security
        sys.modules["fastapi.encoders"] = fastapi_rust.encoders
        sys.modules["fastapi.testclient"] = fastapi_rust.testclient
        sys.modules["fastapi.staticfiles"] = fastapi_rust.staticfiles
        sys.modules["fastapi.templating"] = fastapi_rust.templating
        sys.modules["fastapi.middleware"] = fastapi_rust.middleware
        sys.modules["fastapi.middleware.cors"] = fastapi_rust.middleware.cors
        sys.modules["fastapi.middleware.gzip"] = fastapi_rust.middleware.gzip
        sys.modules["fastapi.middleware.trustedhost"] = fastapi_rust.middleware.trustedhost

    # Import the perf-bench app (uses fastapi_rust.FastAPI under the alias).
    from apps import app  # type: ignore

    if not hasattr(app, "run_native"):
        print(
            "[native_launcher] app has no run_native method — "
            "is the Rust extension up to date?",
            file=sys.stderr,
        )
        return 1

    app.run_native(host=args.host, port=args.port, workers=args.workers)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
