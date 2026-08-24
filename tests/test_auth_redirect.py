"""An expired session must redirect, never render JSON into the page.

`require_user` raises 401 with a `Location` header (`app/auth.py:130-134`).
Browsers only follow `Location` on a 3xx — on a 401 they render the body — and
until `_auth_redirect` was registered there was no exception handler anywhere in
the app. The visible symptom was `{"detail":"Sign in to continue."}` appearing
in the UI when a session lapsed, and for an HTMX poll
(`templates/partials/progress.html`) that JSON was swapped straight into the DOM.

Password sessions are long enough that this rarely fired. Google sessions are
not, so this is load-bearing before SSO goes live.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException, status
from starlette.requests import Request

from app.main import _auth_redirect

pytestmark = pytest.mark.asyncio


def _request(headers: dict[str, str] | None = None) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request({"type": "http", "method": "GET", "path": "/", "headers": raw})


def _unauthorised() -> HTTPException:
    """Exactly what require_user raises."""
    return HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        "Sign in to continue.",
        headers={"Location": "/login"},
    )


async def test_browser_request_gets_a_redirect_it_will_follow():
    response = await _auth_redirect(_request(), _unauthorised())
    assert response.status_code == status.HTTP_303_SEE_OTHER
    assert response.headers["location"] == "/login"


async def test_htmx_request_gets_hx_redirect_on_a_2xx():
    """htmx does not follow a 303 — it swaps the login page into the target
    element. HX-Redirect is the documented escape, and htmx ignores headers on
    error responses, so it has to ride on a 2xx."""
    response = await _auth_redirect(_request({"HX-Request": "true"}), _unauthorised())
    assert response.status_code == status.HTTP_200_OK
    assert response.headers["HX-Redirect"] == "/login"


async def test_htmx_response_body_is_empty():
    """Whatever htmx does with the body, it must never be the JSON detail."""
    response = await _auth_redirect(_request({"HX-Request": "true"}), _unauthorised())
    assert response.body == b""
    assert b"Sign in to continue" not in response.body


async def test_a_401_without_location_is_left_alone():
    """Only require_user's redirecting 401 is special-cased. A bare 401 from
    anywhere else keeps FastAPI's default JSON shape."""
    response = await _auth_redirect(_request(), HTTPException(status.HTTP_401_UNAUTHORIZED, "nope"))
    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    assert b"nope" in response.body


async def test_other_status_codes_are_left_alone():
    """404 and 403 must not be turned into redirects — require_admin_or_404
    returns 404 deliberately so the admin surface is not discoverable."""
    for code in (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND):
        response = await _auth_redirect(_request(), HTTPException(code, "denied"))
        assert response.status_code == code
        assert b"denied" in response.body
