"""Listing, reading and deleting a client, against a real database.

Tested against Postgres rather than a fake for one reason: `domain` is a bare
string in five tables with no foreign key between them, so nothing cascades and
the delete is five explicit statements. The failure mode is not a loud error --
it is one forgotten table, after which the client vanishes from the list while
its rows remain, and the next client registered on that domain silently inherits
the last one's manual marks. Only a real schema can catch that.

Skipped when no database is reachable, so the rest of the suite still runs
anywhere.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.db import repo
from app.db.base import sqlalchemy_url
from app.db.models import Base, RunStatus

pytestmark = pytest.mark.asyncio


async def _engine():
    url = sqlalchemy_url(Settings(_env_file=".env").database_url)
    engine = create_async_engine(url, poolclass=None)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception as exc:
        await engine.dispose()
        pytest.skip(f"no database reachable: {type(exc).__name__}")
    return engine


@pytest.fixture
async def session():
    """A session against a scratch schema holding the whole model, dropped after.

    Every table, not a hand-picked subset: the thing under test is whether the
    delete reaches all of them, and seeding only the tables I remembered would
    test my memory rather than the code.
    """
    engine = await _engine()
    schema = f"test_client_{uuid.uuid4().hex[:8]}"

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


async def seed(session, domain: str, *, runs: int = 1, marks: int = 2, metrics: int = 3) -> None:
    """A client with something in every table the delete has to reach."""
    from app.core.metrics import PageMetrics
    from app.core.models import PageEntry

    await repo.save_site_config(
        session, domain, plan={"site_pattern": "agency"}, max_pages=50, updated_by="t@x.com"
    )
    for n in range(runs):
        run = await repo.create_run(session, f"https://{domain}", "t@x.com")
        await repo.set_status(session, run, RunStatus.COMPLETE)
        await repo.replace_pages(
            session,
            run.id,
            [PageEntry(url=f"https://{domain}/p{n}-{i}", title=f"P{i}") for i in range(4)],
        )
    for n in range(marks):
        await repo.set_mark(session, domain, f"component-{n}", "t@x.com")
    await repo.replace_site_metrics(
        session,
        domain,
        {
            f"https://{domain}/m{i}": PageMetrics(
                url=f"https://{domain}/m{i}", clicks=i, impressions=i * 10
            )
            for i in range(metrics)
        },
        source="upload",
    )
    await repo.save_snapshot(session, domain, probe={"x": 1}, readiness={"score": 42}, tech={})
    # A share link, because the delete has to reach this table too. The docstring
    # above says "something in every table" and that is only true if it keeps
    # being made true -- a table seeded with nothing makes
    # `test_the_preview_counts_what_the_delete_removes` pass against a column
    # that is always zero, which is the shape of a vacuous test.
    await repo.create_share_link(
        session,
        domain=domain,
        section="report",
        expires_at=datetime.now(UTC) + timedelta(days=30),
        created_by="t@x.com",
    )
    await session.commit()


# -- the picker that did not exist -------------------------------------------


async def test_every_configured_client_is_listed(session):
    """`nav.py` described links as pointing at "the picker". There was no picker."""
    await seed(session, "alpha.example")
    await seed(session, "beta.example")

    listed = {c.domain for c in await repo.list_site_configs(session)}

    assert listed == {"alpha.example", "beta.example"}


async def test_a_client_with_no_runs_is_still_listed(session):
    """The exact client the old index could never show.

    Reaching a client meant finding one of their runs in the most recent forty.
    A client onboarded but not yet crawled had no row anywhere in the UI.
    """
    await repo.save_site_config(
        session, "fresh.example", plan={}, max_pages=0, updated_by="t@x.com"
    )
    await session.commit()

    assert "fresh.example" in {c.domain for c in await repo.list_site_configs(session)}


async def test_a_label_survives_a_later_plan_approval(session):
    """`save_site_config` is called on every plan approval, with no label.

    An unguarded assignment would blank the name an operator typed the moment the
    next run was approved.
    """
    await repo.save_site_config(
        session, "x.example", plan={}, max_pages=0, updated_by="t@x.com", label="Big Client"
    )
    await repo.save_site_config(
        session, "x.example", plan={"v": 2}, max_pages=9, updated_by="t@x.com"
    )
    await session.commit()

    config = await repo.load_site_config(session, "x.example")

    assert config.label == "Big Client"
    assert config.plan == {"v": 2}, "the plan still updates"


# -- the delete --------------------------------------------------------------


async def test_the_preview_counts_what_the_delete_removes(session):
    """The guard against a forgotten table.

    If these two ever disagree, an operator confirmed one number and a different
    amount of data went.
    """
    await seed(session, "doomed.example", runs=2, marks=2, metrics=3)

    preview = await repo.preview_client_deletion(session, "doomed.example")
    removed = await repo.delete_client(session, "doomed.example")
    await session.commit()

    assert preview == removed
    assert preview.runs == 2
    assert preview.pages == 8, "four pages per run, reached through the run"
    assert preview.share_links == 1, "seeded, or this column proves nothing"
    assert preview.marks == 2
    assert preview.metric_rows == 3
    assert preview.snapshots == 1
    assert preview.config == 1


async def test_nothing_belonging_to_the_client_survives(session):
    """Read every table back, rather than trusting the returned counts."""
    await seed(session, "doomed.example")
    await repo.delete_client(session, "doomed.example")
    await session.commit()

    assert await repo.load_site_config(session, "doomed.example") is None
    assert await repo.load_snapshot(session, "doomed.example") is None
    assert await repo.load_marks(session, "doomed.example") == {}
    assert await repo.load_site_metrics(session, "doomed.example") == {}
    assert await repo.latest_complete_run(session, "doomed.example") is None
    assert await repo.list_site_configs(session) == []


async def test_deleting_one_client_leaves_the_other_untouched(session):
    """The inheritance bug this test exists to prevent.

    Without a foreign key, a `delete` whose predicate is slightly wrong takes
    another client's rows with it and nothing complains.
    """
    await seed(session, "doomed.example", runs=1, marks=2, metrics=3)
    await seed(session, "keeper.example", runs=2, marks=1, metrics=5)

    await repo.delete_client(session, "doomed.example")
    await session.commit()

    survivor = await repo.preview_client_deletion(session, "keeper.example")

    assert survivor.runs == 2
    assert survivor.pages == 8
    assert survivor.marks == 1
    assert survivor.metric_rows == 5
    assert survivor.snapshots == 1
    assert survivor.config == 1
    assert (await repo.load_site_config(session, "keeper.example")) is not None


async def test_deleting_a_client_that_does_not_exist_is_a_no_op(session):
    """A double-submitted confirm form must not be an error page."""
    await seed(session, "keeper.example")

    removed = await repo.delete_client(session, "never-existed.example")
    await session.commit()

    assert removed.total == 0
    assert removed.summary() == "no stored data"
    assert (await repo.load_site_config(session, "keeper.example")) is not None


async def test_the_summary_names_what_goes_rather_than_a_bare_total(session):
    """The confirm screen has to say what is being destroyed, in words."""
    await seed(session, "doomed.example", runs=1, marks=1, metrics=2)

    summary = (await repo.preview_client_deletion(session, "doomed.example")).summary()

    assert "1 run" in summary
    assert "4 crawled pages" in summary
    assert "1 manual mark" in summary
    assert "2 search-metric rows" in summary


# -- the snapshot cache ------------------------------------------------------


async def test_an_unchecked_domain_has_no_snapshot_rather_than_an_empty_one(session):
    """None means "not checked". It must never be confused with "found nothing"."""
    assert await repo.load_snapshot(session, "unchecked.example") is None


async def test_refreshing_replaces_the_snapshot_and_moves_its_timestamp(session):
    """The failure that would matter: a replaced row keeping its original time.

    Every page reports the snapshot's age, so a stale `fetched_at` would claim
    the data is fresher than it is -- wrong in the only direction that misleads.
    """
    first = await repo.save_snapshot(
        session, "x.example", probe={"v": 1}, readiness={"score": 42}, tech={}
    )
    await session.commit()
    was = first.fetched_at

    second = await repo.save_snapshot(
        session, "x.example", probe={"v": 2}, readiness={"score": 53}, tech={}
    )
    await session.commit()

    assert second.probe == {"v": 2}
    assert second.readiness == {"score": 53}
    assert second.fetched_at > was
    assert (await repo.preview_client_deletion(session, "x.example")).snapshots == 1, (
        "replaced, not appended"
    )


# -- interactive LLM spend ----------------------------------------------------


async def test_a_recorded_call_reaches_the_costs_page(session):
    """Until 2026-08-24 interactive spend reached nothing.

    Three routes built an `LLMClient` and let the `LLMUsage` be
    garbage-collected, so `/admin` reported them as not having happened rather
    than as unpriced -- on the most expensive configured model.
    """
    from app.llm.client import LLMUsage, Stage

    usage = LLMUsage()
    usage.record(Stage.CHAT, 1_200, 300, model="gpt-4o")
    await repo.record_spend(session, usage, domain="x.example", spent_by="a@b.c")
    await session.commit()

    rows = await repo.interactive_spend_since(session, days=1)

    assert len(rows) == 1
    assert rows[0].model == "gpt-4o"
    assert rows[0].prompt_tokens == 1_200
    assert rows[0].completion_tokens == 300
    assert rows[0].domain == "x.example"


async def test_tokens_are_stored_and_dollars_are_not(session):
    """So a rate correction reprices history instead of leaving old rows wrong."""
    from app.core.pricing import rate_for
    from app.llm.client import LLMUsage, Stage

    usage = LLMUsage()
    usage.record(Stage.CHAT, 1_000_000, 0, model="gpt-4o")
    await repo.record_spend(session, usage, domain="x.example")
    await session.commit()

    row = (await repo.interactive_spend_since(session, days=1))[0]
    assert not hasattr(row, "usd"), "priced at read time, never stored"

    input_rate, _ = rate_for(row.model)
    assert (row.prompt_tokens / 1_000_000) * input_rate == input_rate


async def test_a_refusal_that_cost_nothing_is_still_recorded(session):
    """A fallback looks identical to a success on a bill unless it is visible."""
    from app.llm.client import LLMUsage, Stage

    usage = LLMUsage()
    usage.record_fallback(Stage.CHAT, "no OPENAI_API_KEY configured")
    await repo.record_spend(session, usage, domain="x.example")
    await session.commit()

    rows = await repo.interactive_spend_since(session, days=1)

    assert len(rows) == 1
    assert rows[0].prompt_tokens == 0
    assert "no OPENAI_API_KEY configured" in rows[0].fallbacks[0]


async def test_the_daily_count_is_per_domain(session):
    """One busy client must not spend another client's allowance."""
    from app.llm.client import LLMUsage, Stage

    for domain in ("busy.example", "busy.example", "quiet.example"):
        usage = LLMUsage()
        usage.record(Stage.CHAT, 10, 5, model="gpt-4o")
        await repo.record_spend(session, usage, domain=domain)
    await session.commit()

    assert await repo.spend_today(session, "busy.example") == 2
    assert await repo.spend_today(session, "quiet.example") == 1
    assert await repo.spend_today(session, "never.example") == 0


async def test_deleting_a_client_takes_its_spend_history(session):
    """`llm_spend` is keyed on a bare domain like everything else here."""
    from app.llm.client import LLMUsage, Stage

    usage = LLMUsage()
    usage.record(Stage.CHAT, 10, 5, model="gpt-4o")
    await repo.record_spend(session, usage, domain="doomed.example")
    await repo.save_site_config(
        session, "doomed.example", plan={}, max_pages=0, updated_by="t@x.com"
    )
    await session.commit()

    await repo.delete_client(session, "doomed.example")
    await session.commit()

    assert await repo.spend_today(session, "doomed.example") == 0


async def test_a_share_link_does_not_outlive_its_client(session):
    """Deleting a client must kill its links, and it must fail closed.

    A token that survived would point at a domain with no data -- or worse, at a
    domain somebody later re-adds, handing a former client's contact a live view
    of the new one. Asserted through the resolver rather than the count, because
    the count is what a forgotten `delete()` would leave looking correct.
    """
    from datetime import UTC, datetime, timedelta

    await seed(session, "gone.example")
    _link, token = await repo.create_share_link(
        session,
        domain="gone.example",
        section="handover",
        expires_at=datetime.now(UTC) + timedelta(days=30),
        created_by="t@x.com",
    )
    await session.commit()
    assert await repo.resolve_share_link(session, token) is not None

    await repo.delete_client(session, "gone.example")
    await session.commit()

    assert await repo.resolve_share_link(session, token) is None
