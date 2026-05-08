"""Launch the ws_app under hyperfastapi.run_native()."""

from __future__ import annotations

import argparse
import os
import sys


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--workers", type=int, default=1)
    args = parser.parse_args()

    os.environ.setdefault("HYPERFASTAPI_AS_FASTAPI", "1")

    if os.environ.get("HYPERFASTAPI_AS_FASTAPI") == "1":
        import hyperfastapi  # noqa: F401

        sys.modules["fastapi"] = hyperfastapi

    from ws_app import app  # type: ignore

    app.run_native(host=args.host, port=args.port, workers=args.workers)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
