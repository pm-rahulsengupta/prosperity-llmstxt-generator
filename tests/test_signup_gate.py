"""The one-signup rule, against a real database.

This is the security boundary of a publicly reachable deployment, so it is tested
against Postgres rather than a mock: the rule is enforced by an advisory lock and
a re-check inside a transaction, and neither of those exists in a fake.

Skipped when no database is reachable, so the rest of the suite still runs
anywhere.
"""

from __future__ import annotations

import asyncio
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

    A separate schema rather than the real tables: this test creates and deletes
    accounts, and it must never be able to empty a developer's `users` table and
    thereby reopen signup on their instance.
    """
    engine = await _engine()
    schema = f"test_signup_{uuid.uuid4().hex[:8]}"

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


async def test_the_first_signup_becomes_admin(sessions):
    session = await sessions()
    user = await accounts.claim_instance(
        session, "owner@prosperitymedia.com.au", "a-long-enough-pw"
    )
    await session.commit()

    assert user.is_admin is True
    assert user.created_by == "self"
    await session.close()


async def test_the_second_signup_is_refused(sessions):
    session = await sessions()
    await accounts.claim_instance(session, "owner@prosperitymedia.com.au", "a-long-enough-pw")
    await session.commit()

    with pytest.raises(accounts.SignupClosed):
        await accounts.claim_instance(session, "stranger@example.com", "another-long-pw")
    await session.close()


async def test_concurrent_signups_produce_exactly_one_account(sessions):
    """The race geo-tracker's check-then-act version leaves open.

    Two signups starting at the same moment both read zero users unless something
    serialises them. The advisory lock does; without it this test produces two
    admins, each believing it owns the instance.
    """
    session_a = await sessions()
    session_b = await sessions()

    async def claim(session, email):
        try:
            user = await accounts.claim_instance(session, email, "a-long-enough-pw")
            await session.commit()
            return user
        except accounts.SignupClosed:
            await session.rollback()
            return None

    results = await asyncio.gather(
        claim(session_a, "first@prosperitymedia.com.au"),
        claim(session_b, "second@prosperitymedia.com.au"),
        return_exceptions=True,
    )
    winners = [r for r in results if isinstance(r, User)]

    assert len(winners) == 1, f"expected exactly one account, got {results}"

    checker = await sessions()
    assert await accounts.count_users(checker) == 1
    for session in (session_a, session_b, checker):
        await session.close()


async def test_an_admin_can_add_a_teammate_and_they_are_not_an_admin(sessions):
    session = await sessions()
    admin = await accounts.claim_instance(
        session, "owner@prosperitymedia.com.au", "a-long-enough-pw"
    )
    await session.commit()

    mate = await accounts.create_teammate(
        session, admin, "colleague@prosperitymedia.com.au", "another-long-pw"
    )
    await session.commit()

    assert mate.is_admin is False
    assert mate.created_by == admin.email
    assert await accounts.count_users(session) == 2
    await session.close()


async def test_a_non_admin_cannot_add_accounts(sessions):
    session = await sessions()
    admin = await accounts.claim_instance(
        session, "owner@prosperitymedia.com.au", "a-long-enough-pw"
    )
    await session.commit()
    mate = await accounts.create_teammate(
        session, admin, "colleague@prosperitymedia.com.au", "another-long-pw"
    )
    await session.commit()

    with pytest.raises(PermissionError):
        await accounts.create_teammate(session, mate, "outsider@example.com", "yet-another-pw")
    await session.close()


async def test_authentication_accepts_the_right_password_and_nothing_else(sessions):
    session = await sessions()
    await accounts.claim_instance(session, "owner@prosperitymedia.com.au", "a-long-enough-pw")
    await session.commit()

    assert await accounts.authenticate(session, "owner@prosperitymedia.com.au", "a-long-enough-pw")
    assert not await accounts.authenticate(
        session, "owner@prosperitymedia.com.au", "wrong-password"
    )
    assert not await accounts.authenticate(session, "nobody@example.com", "a-long-enough-pw")
    await session.close()


async def test_email_is_matched_case_insensitively(sessions):
    session = await sessions()
    await accounts.claim_instance(session, "Owner@ProsperityMedia.com.au", "a-long-enough-pw")
    await session.commit()

    assert await accounts.authenticate(session, "owner@prosperitymedia.com.au", "a-long-enough-pw")
    await session.close()


async def test_a_deactivated_account_cannot_sign_in(sessions):
    session = await sessions()
    user = await accounts.claim_instance(
        session, "owner@prosperitymedia.com.au", "a-long-enough-pw"
    )
    user.is_active = False
    await session.commit()

    assert not await accounts.authenticate(
        session, "owner@prosperitymedia.com.au", "a-long-enough-pw"
    )
    await session.close()
