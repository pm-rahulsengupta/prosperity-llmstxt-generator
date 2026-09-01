"""A share link must be the same page for everyone, including us.

`SameSite=Lax` with `path=/` means a staff member's session cookie **is** sent on
a top-level GET to `/share/...`. Without `ShareScope` stripping it, a staff
member checking a link would see a page rendered with their own identity in
scope and conclude it works.

The cookie is forged with `itsdangerous` over the real `SESSION_SECRET`, the same
construction `SessionMiddleware` uses, because a test that fakes the session at a
different layer would not exercise the middleware ordering that makes this work.
"""

from __future__ import annotations

import base64
import json

import pytest
from itsdangerous import TimestampSigner
from starlette.testclient import TestClient

from app.config import Settings
from app.core import share
from tests.conftest import skip_without_database

# No asyncio mark: every test here drives the app through `TestClient`, which
# runs the event loop itself. Marking them async would make pytest-asyncio open a
# second loop around a client that already has one.


def staff_cookie(secret: str) -> str:
    """Exactly what `SessionMiddleware` would have set for a signed-in admin."""
    payload = {"user": {"email": "staff@prosperitymedia.com.au", "name": "Staff", "is_admin": True}}
    data = base64.b64encode(json.dumps(payload).encode()).decode()
    return TimestampSigner(secret).sign(data).decode()


@pytest.fixture
def client(monkeypatch):
    """A live app with share links on, and a database if there is one."""
    # Checked before the app is built. Startup connects, and with no database that
    # stalls inside the `TestClient` context where there is nothing to catch it.
    skip_without_database()
    monkeypatch.setenv("SHARE_LINKS_ENABLED", "true")
    from app.config import get_settings

    get_settings.cache_clear()
    import app.main as main

    with TestClient(main.app) as test_client:
        yield test_client
    get_settings.cache_clear()


@pytest.fixture
def token() -> str:
    return share.new_token()


# -- the isolation property ---------------------------------------------------


def test_a_staff_session_changes_nothing_about_a_share_page(client):
    """The assertion, byte for byte.

    Anything that made the page differ for a signed-in viewer -- a nav, a name in
    a corner, a debug hint -- would mean a staff member testing the link cannot
    see what their client sees.
    """
    secret = Settings(_env_file=".env").session_secret
    url = f"/share/{share.new_token()}"

    anonymous = client.get(url)
    signed_in = client.get(url, cookies={"llmstxt_session": staff_cookie(secret)})

    assert signed_in.status_code == anonymous.status_code
    assert signed_in.text == anonymous.text


def test_visiting_a_share_link_sets_no_cookie(client):
    secret = Settings(_env_file=".env").session_secret

    response = client.get(
        f"/share/{share.new_token()}", cookies={"llmstxt_session": staff_cookie(secret)}
    )

    assert "set-cookie" not in {k.lower() for k in response.headers}


def test_a_staff_member_is_still_signed_in_afterwards(client):
    """The regression `request.session.clear()` would cause.

    Starlette emits a delete-cookie when a session was non-empty in and empty
    out, so the obvious implementation signs the operator out of the app the
    moment they check their own link.
    """
    secret = Settings(_env_file=".env").session_secret
    client.cookies.set("llmstxt_session", staff_cookie(secret))

    client.get(f"/share/{share.new_token()}")

    assert client.cookies.get("llmstxt_session") is not None


def test_a_bad_token_reveals_nothing(client):
    """One response for unknown, expired, revoked, deleted and never-probed."""
    response = client.get(f"/share/{share.new_token()}")

    assert response.status_code == 404
    for leak in ("prosperitymedia", "/sites/", "/clients", "expired", "revoked", "domain"):
        assert leak.lower() not in response.text.lower()


def test_a_malformed_token_is_refused_without_a_query(client):
    """Length and alphabet are constrained on the route, so junk never reaches
    the column and a scan is cheap to turn away."""
    for bad in ("short", "../../etc/passwd", "a" * 200, "has spaces in it"):
        assert client.get(f"/share/{bad}").status_code in (404, 422)


# -- headers ------------------------------------------------------------------


def test_every_share_response_carries_the_security_headers(client):
    """Attached by the middleware, so a route or template cannot forget one.

    Asserted on a 404, deliberately: the error path is the one a template would
    have missed.
    """
    from app.main import SHARE_HEADERS

    response = client.get(f"/share/{share.new_token()}")

    for name, value in SHARE_HEADERS.items():
        assert response.headers.get(name) == value, name


def test_the_referrer_policy_is_no_referrer(client):
    """Called out on its own because it is the one that matters most.

    The URL *is* the credential and the page links outward to the client's own
    site. Under any weaker policy, following one of those links hands the whole
    share URL to a third party.
    """
    response = client.get(f"/share/{share.new_token()}")

    assert response.headers["Referrer-Policy"] == "no-referrer"


def test_the_policy_forbids_scripts_and_forms(client):
    csp = client.get(f"/share/{share.new_token()}").headers["Content-Security-Policy"]

    assert "default-src 'none'" in csp
    assert "script-src" not in csp, "no allowlist: the client templates carry no script"
    assert "form-action 'none'" in csp


def test_robots_disallows_everything_and_names_nothing(client):
    """`Disallow: /share/` would publish the existence of the surface it names."""
    body = client.get("/robots.txt").text

    assert "Disallow: /" in body
    assert "/share" not in body


# -- the flag -----------------------------------------------------------------


def test_share_links_are_off_by_default(monkeypatch):
    monkeypatch.delenv("SHARE_LINKS_ENABLED", raising=False)

    assert Settings(_env_file=None).share_links_enabled is False


def test_a_cleartext_deployment_is_refused():
    """The token is the whole credential, so http is the dangerous case."""
    settings = Settings(
        _env_file=None,
        app_url="http://audit.example.com",
        share_links_enabled=True,
        session_secret="x" * 40,
    )

    with pytest.raises(RuntimeError, match="SHARE_LINKS_ENABLED"):
        settings.assert_deployable()


def test_localhost_is_exempt():
    """Or a developer deletes the clause the first time it blocks them."""
    Settings(
        _env_file=None,
        app_url="http://localhost:3000",
        share_links_enabled=True,
    ).assert_deployable()


def test_the_rule_does_not_fire_when_the_feature_is_off():
    Settings(
        _env_file=None, app_url="http://audit.example.com", share_links_enabled=False
    ).assert_deployable()
