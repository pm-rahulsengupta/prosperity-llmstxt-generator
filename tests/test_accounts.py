"""Password handling and the one-signup rule.

The database-backed half of this (the advisory lock, the second signup being
refused) is covered by `tests/test_signup_gate.py`, which needs Postgres. What is
here runs anywhere.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import ClassVar

import pytest

from app.accounts import (
    MIN_PASSWORD_LENGTH,
    WeakPassword,
    hash_password,
    verify_password,
)
from app.auth import User, user_from_claims
from app.config import Settings, get_settings


def test_a_hash_is_not_the_password():
    hashed = hash_password("correct-horse-battery-staple")
    assert "correct-horse" not in hashed
    assert hashed.startswith("$argon2")


def test_verification_round_trips():
    hashed = hash_password("correct-horse-battery-staple")
    assert verify_password(hashed, "correct-horse-battery-staple")
    assert not verify_password(hashed, "correct-horse-battery-stapl")


def test_two_hashes_of_one_password_differ():
    """Salted, so a repeated password is not visible as a repeated hash."""
    assert hash_password("correct-horse-battery-staple") != hash_password(
        "correct-horse-battery-staple"
    )


def test_short_passwords_are_refused():
    with pytest.raises(WeakPassword):
        hash_password("x" * (MIN_PASSWORD_LENGTH - 1))
    hash_password("x" * MIN_PASSWORD_LENGTH)


def test_verification_against_a_missing_or_junk_hash_is_false_not_an_error():
    """An account with no password (Google-only) must not be loggable-into with one."""
    assert verify_password(None, "anything") is False
    assert verify_password("", "anything") is False
    assert verify_password("not-a-hash", "anything") is False


# -- the Google half --------------------------------------------------------


def claims(**overrides):
    base = {"email": "someone@prosperitymedia.com.au", "email_verified": True, "name": "Someone"}
    return {**base, **overrides}


def test_google_domain_is_read_from_the_token_not_the_request():
    settings = Settings(allowed_email_domains="prosperitymedia.com.au")
    user = user_from_claims(claims(hd="prosperitymedia.com.au"), settings)
    assert user.email == "someone@prosperitymedia.com.au"


def test_a_personal_gmail_is_refused():
    from fastapi import HTTPException

    settings = Settings(allowed_email_domains="prosperitymedia.com.au")
    with pytest.raises(HTTPException) as excinfo:
        user_from_claims(claims(email="someone@gmail.com", hd=""), settings)
    assert excinfo.value.status_code == 403


def test_an_unverified_google_account_is_refused():
    from fastapi import HTTPException

    settings = Settings(allowed_email_domains="prosperitymedia.com.au")
    with pytest.raises(HTTPException):
        user_from_claims(claims(email_verified=False), settings)


def test_google_users_are_not_admins_by_default():
    settings = Settings(allowed_email_domains="prosperitymedia.com.au")
    assert user_from_claims(claims(), settings).is_admin is False


# -- the deploy guard -------------------------------------------------------


def test_https_deploy_refuses_anonymous_access():
    settings = Settings(
        app_url="https://llmstxt.example.com",
        session_secret="a-real-secret-value-here",
        allow_anonymous=True,
    )
    with pytest.raises(RuntimeError) as excinfo:
        settings.assert_deployable()
    assert "ALLOW_ANONYMOUS" in str(excinfo.value)


def test_https_deploy_is_allowed_without_google():
    """Password accounts are a complete way in; Google is optional."""
    Settings(
        app_url="https://llmstxt.example.com",
        session_secret="a-real-secret-value-here",
    ).assert_deployable()


def test_session_identity_carries_the_admin_flag():
    assert User(email="a@b.com", is_admin=True).is_admin
    assert not User(email="a@b.com").is_admin


# -- the admin surface ------------------------------------------------------


async def test_a_non_admin_gets_404_not_403_from_an_admin_route():
    """Copied from geo-tracker: a 403 confirms there is an admin area worth
    attacking, a 404 says nothing. The surface exposes spend and accounts."""
    from fastapi import HTTPException

    from app.auth import User, require_admin_or_404

    with pytest.raises(HTTPException) as excinfo:
        await require_admin_or_404(User(email="member@prosperitymedia.com.au"))
    assert excinfo.value.status_code == 404
    # And it must not name the real reason.
    assert "admin" not in str(excinfo.value.detail).lower()


async def test_an_admin_passes_through():
    from app.auth import User, require_admin_or_404

    account = User(email="owner@prosperitymedia.com.au", is_admin=True)

    assert (await require_admin_or_404(account)).is_admin


# -- authority is read from the database, never from the cookie -----------------
#
# `find_by_email` is stubbed rather than exercised: what changed is the decision
# `require_user` makes about a row, and the query itself is covered by
# `test_signup_gate.py`, which has Postgres.


def _cookie(email: str, **claims):
    class Request:
        session: ClassVar[dict] = {"user": {"email": email, **claims}}

    return Request()


def _row(email: str, *, is_admin: bool = False, is_active: bool = True):
    return SimpleNamespace(email=email, name="", is_admin=is_admin, is_active=is_active)


def _stub(monkeypatch, row):
    async def find_by_email(session, email):
        return row

    monkeypatch.setattr("app.auth.find_by_email", find_by_email)


async def test_the_cookie_cannot_grant_admin_on_its_own(monkeypatch):
    """`sign_in` copies `is_admin` into the session and nothing read it back.

    So the flag was decided once, at sign-in, and frozen for the life of the
    cookie: promoting somebody on /accounts did nothing until they signed out and
    back in, and *revoking* admin did not end the session it was revoked from.
    """
    from app.auth import require_user

    _stub(monkeypatch, _row("member@prosperitymedia.com.au", is_admin=False))

    resolved = await require_user(_cookie("member@prosperitymedia.com.au", is_admin=True), None)

    assert resolved.is_admin is False, "the cookie claimed admin; the row does not"


async def test_a_promotion_takes_effect_without_a_re_login(monkeypatch):
    from app.auth import require_user

    _stub(monkeypatch, _row("member@prosperitymedia.com.au", is_admin=True))

    resolved = await require_user(_cookie("member@prosperitymedia.com.au", is_admin=False), None)

    assert resolved.is_admin is True


async def test_a_deactivated_account_loses_the_session_it_already_had(monkeypatch):
    """`resolve_sso` refuses a deactivated account at the door.

    A session already through the door kept working, because nothing rechecked.
    """
    from fastapi import HTTPException

    from app.auth import require_user

    _stub(monkeypatch, _row("member@prosperitymedia.com.au", is_active=False))

    with pytest.raises(HTTPException) as excinfo:
        await require_user(_cookie("member@prosperitymedia.com.au"), None)

    assert excinfo.value.status_code == 401


async def test_a_session_for_a_deleted_account_is_refused(monkeypatch):
    from fastapi import HTTPException

    from app.auth import require_user

    _stub(monkeypatch, None)

    with pytest.raises(HTTPException) as excinfo:
        await require_user(_cookie("ghost@prosperitymedia.com.au"), None)

    assert excinfo.value.status_code == 401


async def test_anonymous_mode_consults_no_table(monkeypatch):
    """`ALLOW_ANONYMOUS` has no accounts table to read, by definition.

    Looking one up would 401 the local-development identity that `require_user`
    just synthesised.
    """
    from app.auth import require_user

    def explode(session, email):
        raise AssertionError("consulted the accounts table in anonymous mode")

    monkeypatch.setattr("app.auth.find_by_email", explode)
    monkeypatch.setenv("ALLOW_ANONYMOUS", "true")
    get_settings.cache_clear()

    try:
        assert (await require_user(_cookie("irrelevant@example.com"), None)).is_admin
    finally:
        get_settings.cache_clear()
