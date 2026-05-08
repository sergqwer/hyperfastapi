"""Collect system/runtime metadata to attach to every perf result file.

Stored in the `system` block of `tests/results/perf/<timestamp>.json` so two
runs from different machines (or different Python versions) can be compared
or filtered later.
"""

from __future__ import annotations

import os
import platform
import sys
from typing import Any

import psutil


def collect() -> dict[str, Any]:
    vm = psutil.virtual_memory()
    return {
        "cpu_model": platform.processor() or "unknown",
        "cores_logical": os.cpu_count() or 0,
        "cores_physical": psutil.cpu_count(logical=False) or 0,
        "ram_gb": round(vm.total / (1024**3), 2),
        "os": platform.platform(),
        "python_version": sys.version.split()[0],
        "python_implementation": platform.python_implementation(),
    }
