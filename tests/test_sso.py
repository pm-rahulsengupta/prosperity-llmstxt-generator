"""Google sign-in, against a real database.

Tested against Postgres rather than a fake because `resolve_sso` holds the same
advisory lock as `claim_instance`, and a lock does not exist in a mock.

Skipped when no database is reachable, so the rest of the suite still runs
anywhere.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app import accounts
from app.db.models import User
from tests.conftest import engine_or_skip

pytestmark = pytest.mark.asyncio


async def _engine():
    # One shared probe: this block was duplicated verbatim across four files, so
    # the hang it papered over had to be found four times before it was fixed once.
    engine = await engine_or_skip()
    return engine


@pytest.fixture
async def sessions():
    """A session factory against a scratch schema, dropped afterwards.

    A separate schema because these tests create and deactivate accounts, and
    must never be able to touch a developer's real `users` table and thereby
    change who can sign in to their instance.
    """
    engine = await _engine()
    schema = f"test_sso_{uuid.uuid4().hex[:8]}"

    async with engine.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        await connection.execute(text(f'SET search_path TO "{schema}"'))
        await connection.run_sync(lambda c: User.__table__.create(c))

    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def make():
        session = factory()
        await session.execute(text(f'SET search_path TO "{schema}"'))
        return session

    try:
        yield make
    finally:
        async with engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await engine.dispose()


# -- the bug this fixes -------------------------------------------------------


async def test_an_admin_signing_in_with_google_is_still_an_admin(sessions):
    """The defect that making Google primary would have exposed.

    `auth_callback` used to sign in the `User` that `user_from_claims` built from
    the ID token, and that dataclass defaults `is_admin` to False. So an admin
    who used Google lost /admin, /accounts and the client-delete Danger Zone,
    silently, with their account row still saying `is_admin = true`.
    """
    session = await sessions()
    await accounts.claim_instance(session, "owner@prosperitymedia.com.au", "a-long-enough-pw")
    await session.commit()

    resolved = await accounts.resolve_sso(session, "owner@prosperitymedia.com.au", "Owner")

    assert resolved.is_admin is True, "the row decides, not the token"
    await session.close()


async def test_a_deactivated_account_cannot_sign_in_through_google(sessions):
    """`is_active` was read on the password path and never on the Google one."""
    session = await sessions()
    owner = await accounts.claim_instance(
        session, "owner@prosperitymedia.com.au", "a-long-enough-pw"
    )
    teammate = await accounts.create_teammate(
        session, owner, "sacked@prosperitymedia.com.au", "another-long-pw"
    )
    teammate.is_active = False
    await session.commit()

    with pytest.raises(accounts.SignupClosed) as refused:
        await accounts.resolve_sso(session, "sacked@prosperitymedia.com.au")

    assert "deactivated" in str(refused.value)
    await session.close()


# -- provisioning -------------------------------------------------------------


async def test_the_first_google_identity_claims_the_instance(sessions):
    """Mirrors `claim_instance`, and is safer than it.

    `claim_instance` performs no domain check at all, so on a public deployment
    whoever finds the URL first can claim it with any address. A Google claim is
    restricted to the allowed domains, because `user_from_claims` validates the
    domain from the signed token before this is reached.
    """
    session = await sessions()

    first = await accounts.resolve_sso(session, "owner@prosperitymedia.com.au", "Owner")
    await session.commit()

    assert first.is_admin is True
    assert first.created_by == "self"
    await session.close()


async def test_a_later_google_identity_is_not_an_admin(sessions):
    session = await sessions()
    await accounts.resolve_sso(session, "owner@prosperitymedia.com.au")
    await session.commit()

    second = await accounts.resolve_sso(session, "someone@prosperitymedia.com.au")
    await session.commit()

    assert second.is_admin is False
    assert second.created_by == "google-sso"
    await session.close()


async def test_a_provisioned_account_has_no_password(sessions):
    """It must not become usable on the password form.

    `verify_password` refuses a `None` hash, so an SSO-provisioned account cannot
    be signed into with any password, including an empty one.
    """
    session = await sessions()
    user = await accounts.resolve_sso(session, "owner@prosperitymedia.com.au")
    await session.commit()

    assert user.password_hash is None
    assert await accounts.authenticate(session, user.email, "") is None
    assert await accounts.authenticate(session, user.email, "anything-at-all") is None
    await session.close()


async def test_signing_in_twice_does_not_create_a_second_account(sessions):
    session = await sessions()
    await accounts.resolve_sso(session, "owner@prosperitymedia.com.au", "Owner")
    await session.commit()
    await accounts.resolve_sso(session, "owner@prosperitymedia.com.au", "Owner")
    await session.commit()

    assert await accounts.count_users(session) == 1
    await session.close()


async def test_a_provisioned_account_appears_on_the_accounts_page(sessions):
    """The old behaviour granted access with no row at all, so an SSO user was
    invisible to the admin and could not be deactivated."""
    session = await sessions()
    await accounts.resolve_sso(session, "owner@prosperitymedia.com.au")
    await session.commit()

    listed = {u.email for u in await accounts.list_users(session)}

    assert "owner@prosperitymedia.com.au" in listed
    await session.close()


async def test_a_name_from_google_fills_a_blank_one(sessions):
    session = await sessions()
    await accounts.resolve_sso(session, "owner@prosperitymedia.com.au")
    await session.commit()

    updated = await accounts.resolve_sso(session, "owner@prosperitymedia.com.au", "Real Name")
    await session.commit()

    assert updated.name == "Real Name"
    await session.close()


async def test_an_existing_name_is_not_overwritten(sessions):
    session = await sessions()
    await accounts.claim_instance(
        session, "owner@prosperitymedia.com.au", "a-long-enough-pw", name="Chosen"
    )
    await session.commit()

    resolved = await accounts.resolve_sso(session, "owner@prosperitymedia.com.au", "From Google")
    await session.commit()

    assert resolved.name == "Chosen"
    await session.close()


async def test_an_address_is_matched_case_insensitively(sessions):
    session = await sessions()
    await accounts.resolve_sso(session, "owner@prosperitymedia.com.au")
    await session.commit()

    await accounts.resolve_sso(session, "Owner@ProsperityMedia.com.AU")
    await session.commit()

    assert await accounts.count_users(session) == 1
    await session.close()
