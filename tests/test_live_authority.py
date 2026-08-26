"""Authority comes from the row, not from the cookie.

Every other test in the suite runs with `ALLOW_ANONYMOUS`, which returns a
synthetic local admin and never touches the accounts table -- so nothing
exercised the path a deployed instance actually takes. This drives real requests
through the real middleware with a signed cookie, and stubs only the two things
that need a database: the accounts lookup and the session.

The bug it exists to prevent, measured on the live instance: `sign_in` copies
`is_admin` into the session cookie and nothing ever read it back, so the flag was
whatever was true at sign-in and stayed that way for the life of the cookie. An
admin provisioned before a promotion saw no Admin group and no client Danger
Zone, and a deactivated account kept the session it already held.
"""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner
from starlette.middleware.sessions import SessionMiddleware

EMAIL = "member@prosperitymedia.com.au"
SECRET = "t" * 48


class _Scalars(list):
    def all(self):
        return []

    def first(self):
        return None


class _EmptySession:
    """Enough of an `AsyncSession` for a route body to render nothing.

    These tests are about the dependency graph, not about queries.
    """

    async def execute(self, *args, **kwargs):
        return SimpleNamespace(
            scalars=_Scalars,
            scalar_one_or_none=lambda: None,
            scalar_one=lambda: 0,
            all=lambda: [],
            first=lambda: None,
        )

    async def commit(self):
        pass


@pytest.fixture
def live(monkeypatch):
    """A client whose cookie says one thing and whose row says another.

    `row` is mutable, so a test can revoke admin or deactivate the account
    between requests -- which is exactly the case the cookie could not see.
    """
    monkeypatch.setenv("ALLOW_ANONYMOUS", "false")
    monkeypatch.setenv("SESSION_SECRET", SECRET)
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost/unused")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "prosperitymedia.com.au")

    from app.config import get_settings

    get_settings.cache_clear()

    import app.auth as auth
    from app.db.base import get_session
    from app.main import app

    row = SimpleNamespace(email=EMAIL, name="Member", is_admin=True, is_active=True)

    async def find_by_email(session, email):
        return row if email == row.email else None

    monkeypatch.setattr(auth, "find_by_email", find_by_email)
    app.dependency_overrides[get_session] = _EmptySession

    # The secret and cookie name come off the app's own middleware rather than
    # from the environment set above. `app.main` binds `SessionMiddleware` to
    # `settings.session_secret` at *import*, so in a full-suite run whichever
    # test imported it first decides the key -- signing with SECRET here made
    # every request 303 to /login when the file ran after the others, and pass
    # when it ran alone.
    session_middleware = next(m for m in app.user_middleware if m.cls is SessionMiddleware)
    secret = session_middleware.kwargs["secret_key"]
    cookie_name = session_middleware.kwargs["session_cookie"]

    # Minted the way `sign_in` mints one, with the flag as it was at sign-in.
    payload = {"user": {"email": EMAIL, "name": "Member", "picture": "", "is_admin": False}}
    cookie = (
        TimestampSigner(str(secret)).sign(base64.b64encode(json.dumps(payload).encode())).decode()
    )

    # No `with`: the lifespan opens a procrastinate pool against a real database.
    client = TestClient(app)
    client.cookies.set(cookie_name, cookie)
    try:
        yield client, row
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_a_signed_cookie_still_gets_through(live):
    """The floor: resolving the row must not break ordinary sign-in."""
    client, _ = live

    assert client.get("/clients", follow_redirects=False).status_code == 200


def test_the_row_grants_admin_the_cookie_denied(live):
    """The measured defect. The cookie says False; the row says True."""
    client, _ = live

    assert client.get("/accounts", follow_redirects=False).status_code == 200


def test_the_template_sees_the_same_flag_as_the_gate(live):
    """Fixing only `require_admin` would have been half a fix.

    `user.is_admin` is also what draws the Admin nav group and the client Danger
    Zone -- so an admin could have deleted a client by POSTing to the URL while
    the button stayed hidden.
    """
    client, _ = live

    body = client.get("/clients", follow_redirects=False).text

    assert "/admin/runs" in body, "the Admin group is drawn from the same flag"


def test_revoking_admin_takes_effect_on_the_next_request(live):
    client, row = live
    assert client.get("/accounts", follow_redirects=False).status_code == 200

    row.is_admin = False

    assert client.get("/accounts", follow_redirects=False).status_code == 403


def test_deactivating_an_account_ends_the_session_it_already_had(live):
    """`resolve_sso` refuses a deactivated account at the door.

    A session already through the door kept working, because nothing rechecked.
    """
    client, row = live
    row.is_active = False

    response = client.get("/clients", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"
