"""RedirectResponse — Location header, status codes 301/302/307/308."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.testclient import TestClient

app = FastAPI()


@app.get("/default")
def default() -> RedirectResponse:
    return RedirectResponse(url="/target")


@app.get("/perm")
def perm() -> RedirectResponse:
    return RedirectResponse(url="/target", status_code=308)


@app.get("/legacy-perm")
def legacy_perm() -> RedirectResponse:
    return RedirectResponse(url="/target", status_code=301)


@app.get("/found")
def found() -> RedirectResponse:
    return RedirectResponse(url="/target", status_code=302)


@app.get("/target")
def target() -> dict:
    return {"reached": True}


client = TestClient(app)


def test_redirect_default_307() -> None:
    response = client.get("/default", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/target"


def test_redirect_308_permanent() -> None:
    response = client.get("/perm", follow_redirects=False)
    assert response.status_code == 308
    assert response.headers["location"] == "/target"


def test_redirect_301_legacy_permanent() -> None:
    response = client.get("/legacy-perm", follow_redirects=False)
    assert response.status_code == 301


def test_redirect_302_found() -> None:
    response = client.get("/found", follow_redirects=False)
    assert response.status_code == 302


def test_redirect_followed_reaches_target() -> None:
    response = client.get("/default", follow_redirects=True)
    assert response.status_code == 200
    assert response.json() == {"reached": True}


def test_redirect_location_case_insensitive() -> None:
    response = client.get("/default", follow_redirects=False)
    # Both must look the same
    assert response.headers["Location"] == response.headers["location"]
