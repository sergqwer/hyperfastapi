"""jsonable_encoder — convert arbitrary objects into JSON-serializable form.

Phase A: a Python implementation handling the common cases (datetime, UUID,
Path, Enum, set/frozenset, Decimal, Pydantic, IP addresses, bytes). Phase I
optionally rewrites the hot loop in Rust if profiling shows it matters.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import Enum
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network
from pathlib import PurePath
from typing import Any
from uuid import UUID

try:
    from pydantic import BaseModel
except ImportError:
    BaseModel = None  # type: ignore[assignment]


def jsonable_encoder(obj: Any) -> Any:  # noqa: PLR0911 PLR0912
    """Recursively convert `obj` into a structure of JSON-native types
    (dict / list / str / int / float / bool / None).
    """
    # Fast path for already-JSON-native scalars
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj

    # Pydantic v2: prefer model_dump
    if BaseModel is not None and isinstance(obj, BaseModel):
        return jsonable_encoder(obj.model_dump())

    # Date / time
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, time):
        return obj.isoformat()
    if isinstance(obj, timedelta):
        return obj.total_seconds()

    # Decimal
    if isinstance(obj, Decimal):
        # int when exponent is non-negative, else float
        if obj.as_tuple().exponent >= 0:
            return int(obj)
        return float(obj)

    # UUID, Path, IP — string representations
    if isinstance(obj, UUID):
        return str(obj)
    if isinstance(obj, PurePath):
        return str(obj)
    if isinstance(obj, (IPv4Address, IPv6Address, IPv4Network, IPv6Network)):
        return str(obj)

    # Enum unwrap to its value
    if isinstance(obj, Enum):
        return obj.value

    # Bytes → str (UTF-8 best-effort, fallback to base64 in Phase E if needed)
    if isinstance(obj, (bytes, bytearray)):
        try:
            return obj.decode("utf-8")
        except UnicodeDecodeError:
            import base64
            return base64.b64encode(obj).decode("ascii")

    # Collections
    if isinstance(obj, (set, frozenset)):
        return [jsonable_encoder(item) for item in obj]
    if isinstance(obj, (list, tuple)):
        return [jsonable_encoder(item) for item in obj]
    if isinstance(obj, dict):
        return {jsonable_encoder(k): jsonable_encoder(v) for k, v in obj.items()}

    # Patterns (regex)
    import re
    if isinstance(obj, re.Pattern):
        return obj.pattern

    # Unknown — return as-is and let json.dumps fail downstream if needed
    return obj
