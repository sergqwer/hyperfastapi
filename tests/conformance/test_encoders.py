"""jsonable_encoder — datetime, UUID, Path, Enum, set/frozenset, Decimal,
   Pydantic BaseModel, bytes, IPv*Address, recursive structures.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from decimal import Decimal
from enum import Enum
from ipaddress import IPv4Address, IPv6Address
from pathlib import PurePosixPath
from uuid import UUID

import pytest
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel


class Color(Enum):
    RED = "red"
    GREEN = "green"


class Inner(BaseModel):
    label: str
    score: float


class Outer(BaseModel):
    name: str
    inner: Inner
    tags: list[str]


def test_datetime_iso8601() -> None:
    dt = datetime(2026, 5, 8, 14, 30, 0, tzinfo=timezone.utc)
    assert jsonable_encoder(dt) == "2026-05-08T14:30:00+00:00"


def test_date_iso8601() -> None:
    assert jsonable_encoder(date(2026, 1, 31)) == "2026-01-31"


def test_time_iso8601() -> None:
    assert jsonable_encoder(time(13, 45, 30)) == "13:45:30"


def test_uuid_as_string() -> None:
    u = UUID("12345678-1234-5678-1234-567812345678")
    assert jsonable_encoder(u) == "12345678-1234-5678-1234-567812345678"


def test_path_as_string() -> None:
    p = PurePosixPath("/var/data/file.txt")
    assert jsonable_encoder(p) == "/var/data/file.txt"


def test_enum_as_value() -> None:
    assert jsonable_encoder(Color.RED) == "red"
    assert jsonable_encoder(Color.GREEN) == "green"


def test_set_as_list() -> None:
    """Sets are not JSON-native; encoder converts to list. Order is undefined,
    so check membership rather than equality.
    """
    encoded = jsonable_encoder({1, 2, 3})
    assert isinstance(encoded, list)
    assert sorted(encoded) == [1, 2, 3]


def test_frozenset_as_list() -> None:
    encoded = jsonable_encoder(frozenset([1, 2]))
    assert isinstance(encoded, list)
    assert sorted(encoded) == [1, 2]


def test_decimal_as_number() -> None:
    """Decimals encode to numbers (int or float depending on representation)."""
    assert jsonable_encoder(Decimal("3.14")) == 3.14
    assert jsonable_encoder(Decimal("100")) == 100


def test_pydantic_model_dumps_to_dict() -> None:
    item = Outer(name="root", inner=Inner(label="x", score=0.5), tags=["a", "b"])
    encoded = jsonable_encoder(item)
    assert encoded == {
        "name": "root",
        "inner": {"label": "x", "score": 0.5},
        "tags": ["a", "b"],
    }


def test_recursive_structure_preserves_types() -> None:
    """Datetime/UUID inside a Pydantic model inside a list inside a dict — all encoded."""
    payload = {
        "items": [
            Outer(name="a", inner=Inner(label="L", score=1.0), tags=[]),
            {"manual": True, "when": datetime(2026, 1, 1, tzinfo=timezone.utc)},
        ],
        "active": True,
    }
    encoded = jsonable_encoder(payload)
    assert encoded["active"] is True
    assert encoded["items"][0]["name"] == "a"
    assert encoded["items"][1]["when"] == "2026-01-01T00:00:00+00:00"


def test_ipv4_as_string() -> None:
    assert jsonable_encoder(IPv4Address("192.168.1.1")) == "192.168.1.1"


def test_ipv6_as_string() -> None:
    assert jsonable_encoder(IPv6Address("::1")) == "::1"


def test_passthrough_for_basic_types() -> None:
    assert jsonable_encoder("string") == "string"
    assert jsonable_encoder(42) == 42
    assert jsonable_encoder(3.14) == 3.14
    assert jsonable_encoder(True) is True
    assert jsonable_encoder(None) is None
    assert jsonable_encoder([1, "two", 3.0]) == [1, "two", 3.0]
    assert jsonable_encoder({"k": "v"}) == {"k": "v"}
