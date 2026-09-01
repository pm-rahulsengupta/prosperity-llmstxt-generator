"""The `www.` re-key, run against a real schema.

Two spellings of a domain were in use at once: `repo.domain_of` (lowercase, no
`www.`), which `Run.domain` held, and `urlparse(...).netloc`, which `SiteConfig`
and every other client-scoped table held. `client_home` filters runs with
`r.domain == domain` against the path segment, so the client page for any `www.`
site listed no runs at all and the delete guard could not see them.

Tested against Postgres rather than a fake for the same reason `test_client_
lifecycle` is: the interesting behaviour is what the two UNIQUE constraints do
when rows collide, and only a real constraint can refuse a statement. The
migration's own `upgrade()` is executed -- not a copy of its SQL -- so a change to
the file is a change to what these assert.

Skipped when no database is reachable.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import uuid
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.db.models import Base, Run, RunStatus, SiteConfig, SiteSnapshot
from tests.conftest import skip_without_database

MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "app/db/migrations/versions/e5b17c30a9f4_one_domain_key.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("one_domain_key", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def migrated():
    """Seed both spellings into a scratch schema, run `upgrade()`, hand back the rows.

    Synchronous throughout: Alembic's `op` is a sync API, and running the real
    migration matters more here than matching the async style of its neighbours.
    """
    skip_without_database()

    from app.config import Settings
    from app.db.base import sqlalchemy_url

    engine = create_engine(sqlalchemy_url(Settings(_env_file=".env").database_url))
    schema = f"test_domainkey_{uuid.uuid4().hex[:8]}"
    connection = engine.connect()
    connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    connection.execute(text(f'SET search_path TO "{schema}"'))
    Base.metadata.create_all(bind=connection)
    connection.commit()
    connection.execute(text(f'SET search_path TO "{schema}"'))

    now = dt.datetime.now(dt.UTC)
    session = Session(bind=connection)
    session.add_all(
        [
            Run(site_url="https://www.a.example", domain="www.a.example", status=RunStatus.PENDING),
            # Capitals, because `removeprefix` is exact and this is the spelling
            # that produced a third key for one domain.
            Run(site_url="https://B.EXAMPLE", domain="WWW.B.EXAMPLE", status=RunStatus.PENDING),
            Run(site_url="https://c.example", domain="c.example", status=RunStatus.PENDING),
            SiteSnapshot(domain="www.d.example", fetched_at=now - dt.timedelta(days=2)),
            SiteSnapshot(domain="d.example", fetched_at=now),
            SiteSnapshot(domain="www.e.example", fetched_at=now),
            SiteConfig(domain="www.f.example", label="Client F", brief={"answered_by": "y"}),
            SiteConfig(domain="f.example", label="", brief={}),
            SiteConfig(domain="www.g.example", label="G www", brief={"answered_by": "a"}),
            SiteConfig(domain="g.example", label="G bare", brief={"answered_by": "b"}),
            SiteConfig(domain="www.h.example", label="H"),
        ]
    )
    session.commit()
    connection.execute(text(f'SET search_path TO "{schema}"'))

    module = _load_migration()
    module.op = Operations(MigrationContext.configure(connection))
    module.upgrade()
    connection.commit()
    connection.execute(text(f'SET search_path TO "{schema}"'))

    def rows(sql: str) -> list[tuple]:
        return [tuple(r) for r in connection.execute(text(sql)).fetchall()]

    try:
        yield rows
    finally:
        session.close()
        connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        connection.commit()
        connection.close()
        engine.dispose()


def test_indexed_tables_are_re_keyed_including_the_uppercase_one(migrated):
    """`runs` carries several rows per domain, so there is nothing to collide."""
    assert sorted(r[0] for r in migrated("SELECT domain FROM runs")) == [
        "a.example",
        "b.example",
        "c.example",
    ]


def test_a_duplicate_snapshot_keeps_the_newer_one(migrated):
    """Snapshots are a cache, so the older of two is the only wrong answer to keep.

    Safe to delete because every column is re-derivable -- "Check the site now"
    rebuilds the row from one probe.
    """
    assert sorted(r[0] for r in migrated("SELECT domain FROM site_snapshots")) == [
        "d.example",
        "e.example",
    ]


def test_an_empty_config_folds_in_favour_of_the_one_holding_the_brief(migrated):
    """Nothing is lost by dropping a config with no label, no plan and no brief.

    Getting this backwards would leave the client looking un-onboarded, with the
    answers a person typed stranded on an unreachable row.
    """
    configs = dict(migrated("SELECT domain, label FROM site_configs"))

    assert configs["f.example"] == "Client F"
    assert "www.f.example" not in configs


def test_two_configs_that_both_hold_content_are_left_alone(migrated):
    """The migration refuses to pick which brief an operator meant to keep.

    Both rows survive. The un-normalised one is unreachable once the code
    addresses one spelling, which is untidy and loses nothing; the log names it so
    a person can merge it deliberately.
    """
    configs = dict(migrated("SELECT domain, label FROM site_configs"))

    assert configs["g.example"] == "G bare"
    assert configs["www.g.example"] == "G www", "a contended row was silently discarded"


def test_a_lone_www_config_is_simply_re_keyed(migrated):
    configs = dict(migrated("SELECT domain, label FROM site_configs"))

    assert configs["h.example"] == "H"
    assert "www.h.example" not in configs


def test_nothing_else_was_created_or_destroyed(migrated):
    """Five configs in, four out: one folded, one contended pair kept whole."""
    assert len(migrated("SELECT domain FROM site_configs")) == 4


def test_the_migration_refuses_to_pretend_it_can_be_reversed(migrated):
    """The stripped `www.` is recorded nowhere, and the folded rows are gone."""
    module = _load_migration()

    with pytest.raises(NotImplementedError):
        module.downgrade()
