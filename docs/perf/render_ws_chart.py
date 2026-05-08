"""Render WebSocket benchmark chart from docs/perf/ws_results.json."""

from __future__ import annotations

import json
import pathlib

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

ROOT = pathlib.Path(__file__).resolve().parents[2]
RESULTS = ROOT / "docs" / "perf" / "ws_results.json"
OUT_DIR = ROOT / "docs" / "img"

HYPER = "#ff7c2e"
FASTAPI = "#3a7ca5"
ACCENT = "#1e3a5f"

plt.rcParams.update({
    "font.family": "DejaVu Sans",
    "font.size": 11,
    "axes.titlesize": 14,
    "axes.titleweight": "bold",
    "axes.spines.right": False,
    "axes.spines.top": False,
    "axes.grid": True,
    "grid.alpha": 0.25,
    "grid.linestyle": "--",
    "axes.axisbelow": True,
})


def main() -> None:
    data = json.loads(RESULTS.read_text(encoding="utf-8"))
    rows = data["scenarios"]
    conns = [r["connections"] for r in rows]
    hyper = [r["hyperfastapi_msg_s"] for r in rows]
    uvi = [r["uvicorn_msg_s"] for r in rows]

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(13, 5.5))

    # --- Throughput by connection count ---
    x = np.arange(len(conns))
    w = 0.38
    ax1.bar(x - w/2, uvi, w, label="FastAPI + uvicorn", color=FASTAPI)
    ax1.bar(x + w/2, hyper, w, label="hyperfastapi + run_native", color=HYPER)
    for i, (h, u) in enumerate(zip(hyper, uvi)):
        ax1.text(i + w/2, h + max(hyper)*0.01, f"{int(h):,}",
                 ha="center", fontsize=9, color=ACCENT, weight="bold")
        ax1.text(i - w/2, u + max(hyper)*0.01, f"{int(u):,}",
                 ha="center", fontsize=9, color=ACCENT)
    ax1.set_xticks(x)
    ax1.set_xticklabels([str(c) for c in conns])
    ax1.set_xlabel("concurrent WebSocket connections")
    ax1.set_ylabel("throughput (echo round-trips / sec)")
    ax1.set_title("WebSocket echo throughput", loc="left")
    ax1.legend(loc="upper left", frameon=False)

    # --- Max latency by connection count ---
    hyper_lat = [r["hyperfastapi_max_latency_ms"] for r in rows]
    uvi_lat = [r["uvicorn_max_latency_ms"] for r in rows]
    ax2.plot(conns, uvi_lat, "o-", color=FASTAPI, label="FastAPI + uvicorn",
             markersize=7, linewidth=2)
    ax2.plot(conns, hyper_lat, "o-", color=HYPER, label="hyperfastapi + run_native",
             markersize=7, linewidth=2)
    for c, l in zip(conns, hyper_lat):
        ax2.text(c, l + 0.5, f"{l:.1f}ms", ha="center", fontsize=8, color=HYPER, weight="bold")
    for c, l in zip(conns, uvi_lat):
        ax2.text(c, l + 0.5, f"{l:.1f}ms", ha="center", fontsize=8, color=FASTAPI)
    ax2.set_xscale("log", base=2)
    ax2.set_xticks(conns)
    ax2.set_xticklabels([str(c) for c in conns])
    ax2.set_xlabel("concurrent WebSocket connections (log scale)")
    ax2.set_ylabel("max latency (ms)")
    ax2.set_title("WebSocket max round-trip latency", loc="left")
    ax2.legend(loc="upper left", frameon=False)

    fig.text(0.99, 0.01, "Windows / 64-byte payload / Rust ws-bench client (tokio-tungstenite)",
             ha="right", fontsize=8, color="#666")
    fig.tight_layout()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / "ws_perf.png"
    fig.savefig(out, dpi=140, bbox_inches="tight")
    plt.close(fig)
    print(f"  wrote {out}")


if __name__ == "__main__":
    main()
