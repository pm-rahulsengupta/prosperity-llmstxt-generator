from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Tests run against the source tree, not an installed wheel.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings
from app.runtime import configure_event_loop

# psycopg cannot run async on the loop Python picks by default on Windows, and
# pytest-asyncio creates that loop. Without this the database-backed tests skip
# themselves with "no database reachable" on this machine and nowhere else.
configure_event_loop()
from app.core.models import PageEntry

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never let a developer's `.env` or shell decide what the tests assert.

    `Settings` reads `.env` and `.env.local` by design, which is right for the app
    and wrong for a test suite: the deploy-safety test passed on a clean checkout
    and silently stopped testing anything the moment a real SESSION_SECRET existed
    on disk. Both sources are cut here, for every test, so the suite behaves the
    same on this laptop and in CI.
    """
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)


@pytest.fixture
def sf_csv() -> str:
    return (FIXTURES / "screaming_frog_internal_all.csv").read_text(encoding="utf-8-sig")


@pytest.fixture
def page() -> PageEntry:
    """A mid-importance page with every signal present."""
    return PageEntry(
        url="https://example.com/docs/quickstart",
        title="Quick Start Guide | Example",
        description="Get started with Example in five minutes.",
        word_count=800,
        crawl_depth=2,
        unique_inlinks=12,
        link_score=60,
    )


#: How long the database probe may take before the suite concludes there is none.
#:
#: Needed because psycopg's async connect does not fail fast when nothing is
#: listening -- it hangs. Measured here: a TCP connect to 127.0.0.1:5432 is refused
#: in under a millisecond, `engine.connect()` never returns, the `except` below
#: therefore never runs, and the skip never happens. Four files stalled for tens of
#: minutes each instead of skipping in milliseconds, which made the suite look dead
#: rather than database-less.
DB_PROBE_TIMEOUT = 3

#: Remembered across the session once the answer is known.
#:
#: The probe is per-test, and a refused connection costs the full timeout every
#: time: 16 tests in one file spent 98 seconds establishing the same fact sixteen
#: times. There is no case where a database appears midway through a run, so the
#: first "no" is the answer for the rest of it.
_NO_DATABASE: str | None = None


async def engine_or_skip():
    """An engine against the configured database, or skip if there is not one.

    One copy, shared. This was duplicated verbatim in four test files, so the hang
    above had to be found and fixed four times -- and a fifth file boots the whole
    app and hits the same wall from a different direction.
    """
    global _NO_DATABASE

    import pytest
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.config import Settings
    from app.db.base import sqlalchemy_url

    if _NO_DATABASE is not None:
        pytest.skip(_NO_DATABASE)

    url = sqlalchemy_url(Settings(_env_file=".env").database_url)
    engine = create_async_engine(
        url, poolclass=None, connect_args={"connect_timeout": DB_PROBE_TIMEOUT}
    )
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception as exc:
        await engine.dispose()
        _NO_DATABASE = f"no database reachable: {type(exc).__name__}"
        pytest.skip(_NO_DATABASE)
    return engine


def skip_without_database() -> None:
    """Sync counterpart, for fixtures that boot the whole app rather than an engine.

    `test_share_isolation` builds a `TestClient`, so the connection attempt happens
    inside app startup where there is no engine to probe and no exception to catch
    -- it simply stalls, once per test. A socket check answers the same question in
    a millisecond and needs no event loop.

    Deliberately only a reachability test. A port that accepts is not proof of a
    working database, but a port that refuses is proof of the opposite, and that is
    the case worth being fast about.
    """
    global _NO_DATABASE

    import socket
    from urllib.parse import urlparse

    import pytest

    from app.config import Settings

    if _NO_DATABASE is not None:
        pytest.skip(_NO_DATABASE)

    parsed = urlparse(Settings(_env_file=".env").database_url)
    sock = socket.socket()
    sock.settimeout(DB_PROBE_TIMEOUT)
    try:
        sock.connect((parsed.hostname or "localhost", parsed.port or 5432))
    except OSError as exc:
        _NO_DATABASE = f"no database reachable: {type(exc).__name__}"
        pytest.skip(_NO_DATABASE)
    finally:
        sock.close()
