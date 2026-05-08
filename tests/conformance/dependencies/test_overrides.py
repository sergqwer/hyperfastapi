"""dependency_overrides — replacing dependencies for testing."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient


def real_db() -> str:
    return "real-db"


def fake_db() -> str:
    return "fake-db"


def another_fake() -> str:
    return "another-fake"


app = FastAPI()


@app.get("/data")
def get_data(db: Annotated[str, Depends(real_db)]) -> dict:
    return {"db": db}


client = TestClient(app)


def test_override_swaps_dependency() -> None:
    app.dependency_overrides[real_db] = fake_db
    try:
        response = client.get("/data")
        assert response.status_code == 200
        assert response.json() == {"db": "fake-db"}
    finally:
        app.dependency_overrides.clear()


def test_no_override_uses_real_dependency() -> None:
    """Without an override, the real dep is used."""
    app.dependency_overrides.clear()
    response = client.get("/data")
    assert response.json() == {"db": "real-db"}


def test_override_can_be_changed_mid_test() -> None:
    """The dict is mutable; later overrides take effect on subsequent requests."""
    try:
        app.dependency_overrides[real_db] = fake_db
        assert client.get("/data").json() == {"db": "fake-db"}

        app.dependency_overrides[real_db] = another_fake
        assert client.get("/data").json() == {"db": "another-fake"}
    finally:
        app.dependency_overrides.clear()


def test_override_removed_falls_back_to_real() -> None:
    try:
        app.dependency_overrides[real_db] = fake_db
        assert client.get("/data").json() == {"db": "fake-db"}
    finally:
        app.dependency_overrides.clear()
    # After clear, real dep is used again
    assert client.get("/data").json() == {"db": "real-db"}


def test_override_does_not_affect_other_apps() -> None:
    """`dependency_overrides` is per-app; another FastAPI() is unaffected."""
    other_app = FastAPI()

    @other_app.get("/data")
    def other_data(db: Annotated[str, Depends(real_db)]) -> dict:
        return {"db": db}

    other_client = TestClient(other_app)

    app.dependency_overrides[real_db] = fake_db
    try:
        # Original app sees the override
        assert client.get("/data").json() == {"db": "fake-db"}
        # Other app does NOT
        assert other_client.get("/data").json() == {"db": "real-db"}
    finally:
        app.dependency_overrides.clear()


# Helpers for test_override_with_yield_dep — must be module-level so
# `from __future__ import annotations` + Annotated[..., Depends(...)] resolves
# correctly at route-registration time (FastAPI uses get_type_hints).
_yield_log: list[str] = []


def real_session():
    _yield_log.append("real-open")
    yield "real"
    _yield_log.append("real-close")


def fake_session():
    _yield_log.append("fake-open")
    yield "fake"
    _yield_log.append("fake-close")


_yield_app = FastAPI()


@_yield_app.get("/sess")
def get_sess(s: Annotated[str, Depends(real_session)]) -> dict:
    return {"s": s}


def test_override_with_yield_dep() -> None:
    """Overrides also work for yield-style deps; teardown of the override runs."""
    _yield_log.clear()
    _yield_app.dependency_overrides[real_session] = fake_session
    try:
        response = TestClient(_yield_app).get("/sess")
        assert response.json() == {"s": "fake"}
        assert "fake-open" in _yield_log
        assert "fake-close" in _yield_log
        assert "real-open" not in _yield_log
    finally:
        _yield_app.dependency_overrides.clear()
