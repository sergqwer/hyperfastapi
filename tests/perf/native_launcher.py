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
    # hyperfastapi when HYPERFASTAPI_AS_FASTAPI=1.
    if os.environ.get("HYPERFASTAPI_AS_FASTAPI") == "1":
        import hyperfastapi  # noqa: F401
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

    # Import the perf-bench app (uses hyperfastapi.FastAPI under the alias).
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
