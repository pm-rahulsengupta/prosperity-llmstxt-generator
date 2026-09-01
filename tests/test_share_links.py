"""Share links against a real database.

The token is the whole authorisation -- there is no ownership model on any table
in this schema -- so these are the tests that say what a token may and may not
reach. Skipped when no database is available, like the other lifecycle tests.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core import share
from app.db import repo
from app.db.models import Base
from tests.conftest import engine_or_skip

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def session():
    """A scratch schema, dropped afterwards.

    Separate from the developer's own tables because these tests mint and revoke
    credentials, and must never be able to touch a real client's links.
    """
    # One shared probe: this block was duplicated verbatim across four files, so
    # the hang it papered over had to be found four times before it was fixed once.
    engine = await engine_or_skip()

    schema = f"test_share_{uuid.uuid4().hex[:8]}"
    async with engine.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        await connection.execute(text(f'SET search_path TO "{schema}"'))
        await connection.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    made = factory()
    await made.execute(text(f'SET search_path TO "{schema}"'))
    try:
        yield made
    finally:
        await made.close()
        async with engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await engine.dispose()


async def mint(session, domain="a.example", section="report", days=30, by="staff@x.com"):
    link, token = await repo.create_share_link(
        session,
        domain=domain,
        section=section,
        expires_at=datetime.now(UTC) + timedelta(days=days),
        created_by=by,
    )
    await session.commit()
    return link, token


# -- what the token authorises ------------------------------------------------


async def test_a_token_resolves_to_its_own_domain_and_nothing_else(session):
    """The property the whole design rests on.

    Neither the domain nor the section is in the URL, so there is nothing for a
    client to edit. This asserts the row is where they come from.
    """
    _link, token = await mint(session, domain="a.example", section="handover")

    resolved = await repo.resolve_share_link(session, token)

    assert resolved.domain == "a.example"
    assert resolved.section == "handover"


async def test_one_clients_token_cannot_reach_another_client(session):
    await mint(session, domain="a.example")
    _link_b, token_b = await mint(session, domain="b.example")

    assert (await repo.resolve_share_link(session, token_b)).domain == "b.example"


async def test_an_unknown_token_resolves_to_nothing(session):
    await mint(session)

    assert await repo.resolve_share_link(session, share.new_token()) is None


# -- storage ------------------------------------------------------------------


async def test_the_plaintext_token_is_never_stored(session):
    """A database dump must not contain live credentials.

    This is what the hash defends against -- not guessing, which 256 bits already
    settles. See `app.core.share`.
    """
    link, token = await mint(session)

    stored = {str(value) for value in link.__dict__.values() if isinstance(value, str)}

    assert token not in stored
    assert link.token_hash == share.token_hash(token)
    assert len(link.token_hash) == 64


async def test_a_token_is_long_enough_that_a_throttle_is_not_the_defence(session):
    _link, token = await mint(session)

    assert len(token) == share.TOKEN_CHARS
    assert share.TOKEN_BYTES * 8 == 256


# -- lifecycle ----------------------------------------------------------------


async def test_an_expired_link_still_resolves_but_reads_as_expired(session):
    """`resolve` deliberately does not filter on state.

    The handler needs to tell unknown from expired from revoked for the log,
    while returning one identical response to the client. Folding the predicate
    into the query would throw that away.
    """
    _link, token = await repo.create_share_link(
        session,
        domain="a.example",
        section="report",
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
        created_by="staff@x.com",
    )
    await session.commit()

    resolved = await repo.resolve_share_link(session, token)

    assert resolved is not None
    assert resolved.state(datetime.now(UTC)) == "expired"


async def test_a_revoked_link_reads_as_revoked(session):
    link, token = await mint(session)

    await repo.revoke_share_link(session, link.id, by="staff@x.com", now=datetime.now(UTC))
    await session.commit()

    assert (await repo.resolve_share_link(session, token)).state(datetime.now(UTC)) == "revoked"


async def test_revoking_twice_keeps_the_first_revocation(session):
    """The audit trail says who closed it, and the second click must not rewrite that."""
    link, _token = await mint(session)
    first = datetime.now(UTC)

    await repo.revoke_share_link(session, link.id, by="alice@x.com", now=first)
    await repo.revoke_share_link(session, link.id, by="bob@x.com", now=first + timedelta(hours=1))
    await session.commit()

    assert link.revoked_by == "alice@x.com"


async def test_there_is_no_link_without_an_expiry(session):
    """A link that never expires is the one still live in a forwarded email in
    three years, so the column refuses it rather than the UI."""
    from app.db.models import ShareLink

    assert ShareLink.__table__.columns["expires_at"].nullable is False


# -- viewing ------------------------------------------------------------------


async def test_a_view_is_counted_and_dated(session):
    link, _token = await mint(session)
    now = datetime.now(UTC)

    await repo.record_share_view(session, link, now=now)
    await repo.record_share_view(session, link, now=now + timedelta(minutes=5))
    await session.commit()

    assert link.view_count == 2
    assert link.first_viewed_at == now
    assert link.last_viewed_at == now + timedelta(minutes=5)


async def test_nothing_about_the_viewer_is_recorded(session):
    """No IP, no User-Agent, no Referer.

    An IP is personal information about someone with no relationship to us; it
    would routinely be wrong, because mail-security scanners fetch every URL in an
    email before a human sees it; and it buys nothing, because a leaked link is
    remedied by revoking it.
    """
    from app.db.models import ShareLink

    columns = set(ShareLink.__table__.columns.keys())

    assert not (columns & {"ip", "ip_address", "user_agent", "referer", "referrer"})


# -- limits -------------------------------------------------------------------


async def test_live_links_are_counted_without_the_dead_ones(session):
    now = datetime.now(UTC)
    live, _t1 = await mint(session)
    revoked, _t2 = await mint(session)
    await repo.create_share_link(
        session,
        domain="a.example",
        section="report",
        expires_at=now - timedelta(days=1),
        created_by="staff@x.com",
    )
    await repo.revoke_share_link(session, revoked.id, by="staff@x.com", now=now)
    await session.commit()

    assert await repo.live_share_link_count(session, "a.example", now=now) == 1
    assert live.state(now) == "live"


async def test_the_listing_is_newest_first(session):
    await mint(session, section="overview")
    await mint(session, section="handover")

    listed = await repo.list_share_links(session, "a.example")

    assert [item.section for item in listed] == ["handover", "overview"]


async def test_the_sections_a_token_may_name_match_what_can_be_rendered(session):
    """Two enums, kept equal by a test rather than by memory.

    `ShareSection` is authorisation-bearing and lives in the database as a CHECK;
    `SECTION_KEYS` is a rendering concern. A value in one and not the other is a
    link that 404s forever after it has been emailed.
    """
    from app.core.client_report import SECTION_KEYS

    assert {s.value for s in share.ShareSection} == set(SECTION_KEYS)
