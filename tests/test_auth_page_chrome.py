"""A signed-out visitor must not see the application's navigation.

`base.html` puts the header inside `{% block chrome %}`, which `login.html` and
`signup.html` blank. The sidebar sits *outside* that block, so it rendered
anyway: the login page shipped the whole nav — "Overview — Pick a client first"
and eleven siblings — stacked above the sign-in form. It leaks the product's
structure to anyone who loads the URL, and it reads as broken.

These tests render the real templates, because the bug was in a template and a
test of the Python would not have caught it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.auth import User
from app.nav import build_nav

TEMPLATES = Path(__file__).resolve().parents[1] / "templates"


@pytest.fixture
def env() -> Environment:
    e = Environment(
        loader=FileSystemLoader(str(TEMPLATES)),
        autoescape=select_autoescape(["html"]),
    )
    e.globals["asset_version"] = lambda _name: "test"
    e.globals["build_nav"] = build_nav
    e.globals["sso_enabled"] = False
    e.globals["allow_anonymous"] = False
    e.globals["llm_enabled"] = True
    e.globals["firecrawl_enabled"] = False
    e.globals["size_check_enabled"] = False
    return e


def _render(env: Environment, name: str, **ctx) -> str:
    return env.get_template(name).render(request=None, **ctx)


# Text that only ever appears in the sidebar.
NAV_MARKERS = ("Pick a client first", "Crawl rules", "Developer handover")


def test_login_page_has_no_sidebar(env):
    html = _render(env, "login.html", user=None, error=None)
    for marker in NAV_MARKERS:
        assert marker not in html, f"sidebar leaked onto the login page: {marker!r}"
    assert 'class="side"' not in html


def test_login_page_still_shows_the_sign_in_form(env):
    """Guard against fixing the leak by breaking the page."""
    html = _render(env, "login.html", user=None, error=None)
    assert 'action="/login"' in html
    assert 'name="password"' in html


def test_signup_page_has_no_sidebar(env):
    html = _render(env, "signup.html", user=None, error=None)
    for marker in NAV_MARKERS:
        assert marker not in html


def test_google_button_appears_only_when_sso_is_configured(env):
    """The button is absent because GOOGLE_CLIENT_ID/SECRET are unset, not
    because it is broken. Pin both directions so a future change to
    `sso_enabled` cannot silently remove the only way in."""
    assert "/login/google" not in _render(env, "login.html", user=None, error=None)

    env.globals["sso_enabled"] = True
    html = _render(env, "login.html", user=None, error=None)
    assert 'href="/login/google"' in html
    assert "Sign in with Google" in html


def test_a_signed_in_page_still_renders_the_sidebar(env):
    """The fix must not remove navigation from the app itself."""
    user = User(email="someone@prosperitymedia.com.au", name="Someone")
    html = _render(env, "base.html", user=user)
    assert 'class="side"' in html


def test_admin_creates_accounts_note_is_scoped_to_the_no_sso_world(env):
    """That note is only true where password accounts are the only door.

    With Google configured a Workspace account IS the way in and needs nobody to
    create anything, so showing "an admin creates them" misdescribes the product
    to exactly the people reading it.
    """
    env.globals["sso_enabled"] = False
    off = _render(env, "login.html", user=None, error=None)
    assert "An admin creates them" in off, "still true when password is the only door"

    env.globals["sso_enabled"] = True
    on = _render(env, "login.html", user=None, error=None)
    assert "An admin creates them" not in on
    assert "self-service signup" not in on
    assert "Any Prosperity Media Google account can sign in." in on
