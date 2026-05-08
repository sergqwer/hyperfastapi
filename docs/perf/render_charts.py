"""Render performance comparison charts from compare_backends.py +
bench_hyper_multiproc.py output JSONs.

Produces three PNGs in docs/img/:
  * perf_single.png    — single-process: FastAPI+uvicorn vs hyperfastapi+hyper
  * perf_multi.png     — multi-process: FastAPI+uvicorn(N workers) vs
                          hyperfastapi+hyper(N procs)
  * perf_speedup.png   — speedup ratio per scenario

Run from repo root:
    python docs/perf/render_charts.py
"""

from __future__ import annotations

import json
import pathlib

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

ROOT = pathlib.Path(__file__).resolve().parents[2]
RESULTS = ROOT / "docs" / "perf" / "results.json"
MULTIPROC = ROOT / "docs" / "perf" / "multiproc.json"
OUT_DIR = ROOT / "docs" / "img"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# Brand colors — orange = hyperfastapi (Rust ferrum), blue = vanilla FastAPI.
HYPER_COLOR = "#ff7c2e"
FASTAPI_COLOR = "#3a7ca5"
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


def _load() -> tuple[dict, dict]:
    return json.loads(RESULTS.read_text(encoding="utf-8")), json.loads(MULTIPROC.read_text(encoding="utf-8"))


def _label(name: str) -> str:
    """Convert scenario name into a friendly y-axis label."""
    mapping = {
        "get_plain": "GET /plain",
        "get_with_query": "GET /with-query",
        "post_validated": "POST /post-validated\n(Pydantic body)",
        "get_with_chain_deps": "GET /with-chain\n(3-step DI)",
        "get_async": "GET /async",
        "get_with_middleware": "GET /with-middleware",
    }
    return mapping.get(name, name)


def chart_single(results: dict) -> None:
    rows = results["scenarios"]
    names = [r["name"] for r in rows]
    fastapi = [r["fastapi_uvicorn_single"] for r in rows]
    hyper = [r["hyperfastapi_hyper_single"] for r in rows]

    fig, ax = plt.subplots(figsize=(10, 5.5))
    y = np.arange(len(names))
    h = 0.38
    ax.barh(y - h/2, fastapi, h, label="FastAPI + uvicorn", color=FASTAPI_COLOR)
    ax.barh(y + h/2, hyper, h, label="hyperfastapi + hyper", color=HYPER_COLOR)

    for i, (f, hh) in enumerate(zip(fastapi, hyper)):
        ax.text(f + max(fastapi + hyper)*0.01, i - h/2, f"{int(f):,}",
                va="center", fontsize=9, color=ACCENT)
        ax.text(hh + max(fastapi + hyper)*0.01, i + h/2, f"{int(hh):,}",
                va="center", fontsize=9, color=ACCENT, weight="bold")

    ax.set_yticks(y)
    ax.set_yticklabels([_label(n) for n in names])
    ax.invert_yaxis()
    ax.set_xlabel("Requests per second (higher is better)")
    ax.set_title("Single-process throughput", loc="left")
    # Headroom for the value labels at the bar tips so the longest bars
    # don't get their numbers clipped at the right edge.
    ax.set_xlim(0, max(fastapi + hyper) * 1.13)
    # Legend below the plot so it never overlaps the long bars.
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.12),
              ncol=2, frameon=False)
    fig.text(0.99, 0.01,
             "Windows / single Python process / bombardier c=100 / 5s per scenario",
             ha="right", fontsize=8, color="#666")
    fig.tight_layout()
    out = OUT_DIR / "perf_single.png"
    fig.savefig(out, dpi=140, bbox_inches="tight")
    plt.close(fig)
    print(f"  wrote {out}")


def chart_multi(results: dict, multiproc: dict) -> None:
    rows = results["scenarios"]
    mp_by_name = {r["name"]: r["rps_total"] for r in multiproc["scenarios"]}
    names = [r["name"] for r in rows if r["name"] in mp_by_name]
    fastapi_multi = [r["fastapi_uvicorn_multi"] for r in rows if r["name"] in mp_by_name]
    hyper_multi = [mp_by_name[r["name"]] for r in rows if r["name"] in mp_by_name]

    fig, ax = plt.subplots(figsize=(10, 5.5))
    y = np.arange(len(names))
    h = 0.38
    ax.barh(y - h/2, fastapi_multi, h, label="FastAPI + uvicorn (N workers)", color=FASTAPI_COLOR)
    ax.barh(y + h/2, hyper_multi, h, label=f"hyperfastapi + hyper ({multiproc['workers']} procs)", color=HYPER_COLOR)

    max_v = max(fastapi_multi + hyper_multi)
    for i, (f, hh) in enumerate(zip(fastapi_multi, hyper_multi)):
        ax.text(f + max_v*0.01, i - h/2, f"{int(f):,}",
                va="center", fontsize=9, color=ACCENT)
        ax.text(hh + max_v*0.01, i + h/2, f"{int(hh):,}",
                va="center", fontsize=9, color=ACCENT, weight="bold")

    ax.axvline(100_000, color="#cc0033", linestyle=":", linewidth=1.2, alpha=0.7,
               label="100k RPS goal")

    ax.set_yticks(y)
    ax.set_yticklabels([_label(n) for n in names])
    ax.invert_yaxis()
    ax.set_xlabel("Requests per second (higher is better)")
    ax.set_title(f"Multi-process throughput (workers = {multiproc['workers']})", loc="left")
    ax.set_xlim(0, max_v * 1.13)
    # Three-item legend below the plot so neither the bars nor the goal
    # line get covered.
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.12),
              ncol=3, frameon=False)
    fig.text(0.99, 0.01,
             "Windows / N independent Python processes / bombardier per process aggregated",
             ha="right", fontsize=8, color="#666")
    fig.tight_layout()
    out = OUT_DIR / "perf_multi.png"
    fig.savefig(out, dpi=140, bbox_inches="tight")
    plt.close(fig)
    print(f"  wrote {out}")


def chart_speedup(results: dict, multiproc: dict) -> None:
    rows = results["scenarios"]
    mp_by_name = {r["name"]: r["rps_total"] for r in multiproc["scenarios"]}
    names = [r["name"] for r in rows]
    speed_single = [
        r["hyperfastapi_hyper_single"] / r["fastapi_uvicorn_single"]
        for r in rows
    ]
    speed_multi = [
        (mp_by_name.get(r["name"]) or r["hyperfastapi_hyper_multi"]) /
        r["fastapi_uvicorn_multi"]
        for r in rows
    ]

    fig, ax = plt.subplots(figsize=(10, 5.5))
    y = np.arange(len(names))
    h = 0.38
    ax.barh(y - h/2, speed_single, h, label="single-process", color="#9bb8c5")
    ax.barh(y + h/2, speed_multi, h, label=f"{multiproc['workers']}-process", color=HYPER_COLOR)

    max_v = max(speed_single + speed_multi)
    for i, (s1, s2) in enumerate(zip(speed_single, speed_multi)):
        ax.text(s1 + max_v*0.01, i - h/2, f"{s1:.1f}×",
                va="center", fontsize=9, color=ACCENT)
        ax.text(s2 + max_v*0.01, i + h/2, f"{s2:.1f}×",
                va="center", fontsize=9, color=ACCENT, weight="bold")

    ax.axvline(1.0, color="#666", linewidth=0.8, alpha=0.5)
    ax.set_yticks(y)
    ax.set_yticklabels([_label(n) for n in names])
    ax.invert_yaxis()
    ax.set_xlabel("Speedup over FastAPI + uvicorn (higher is better)")
    ax.set_title("Throughput speedup vs FastAPI + uvicorn", loc="left")
    ax.set_xlim(0, max_v * 1.13)
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.12),
              ncol=2, frameon=False)
    fig.tight_layout()
    out = OUT_DIR / "perf_speedup.png"
    fig.savefig(out, dpi=140, bbox_inches="tight")
    plt.close(fig)
    print(f"  wrote {out}")


def main() -> None:
    results, multiproc = _load()
    chart_single(results)
    chart_multi(results, multiproc)
    chart_speedup(results, multiproc)


if __name__ == "__main__":
    main()
