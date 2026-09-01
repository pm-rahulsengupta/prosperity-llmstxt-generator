"""Engine, session factory, and the one URL rewrite everything else depends on.

Railway hands out `postgresql://...`. SQLAlchemy's async engine needs an async
driver named in the scheme, and psycopg 3 is only used asynchronously if the URL
says `postgresql+psycopg://`. Without the rewrite the engine silently picks the
sync psycopg2 dialect and the first `await` fails at runtime, in the worker, on
deploy -- not locally, where the same mistake is easy to miss.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings


class Base(DeclarativeBase):
    pass


_SQLALCHEMY_SCHEME = "postgresql+psycopg://"
_BARE_SCHEMES = ("postgresql://", "postgres://", "postgresql+psycopg2://", "postgresql+asyncpg://")


def sqlalchemy_url(url: str) -> str:
    """Name psycopg 3 explicitly in the scheme, whatever Railway or a human supplied.

    One URL serves both engines: the psycopg 3 dialect is sync under
    `create_engine` and async under `create_async_engine`, so the driver choice is
    made by the call, not the string. Leaving the scheme bare is what bites --
    `postgresql://` resolves to psycopg2, which is not installed, and the failure
    surfaces in the migrate step on deploy rather than here.
    """
    if url.startswith(_SQLALCHEMY_SCHEME):
        return url
    for prefix in _BARE_SCHEMES:
        if url.startswith(prefix):
            return _SQLALCHEMY_SCHEME + url[len(prefix) :]
    return url


def psycopg_dsn(url: str) -> str:
    """A plain libpq connection string, for procrastinate.

    procrastinate takes a DSN and hands it to psycopg directly; a SQLAlchemy
    dialect prefix is not a thing libpq understands.
    """
    for prefix in (_SQLALCHEMY_SCHEME, "postgresql+psycopg2://", "postgresql+asyncpg://"):
        if url.startswith(prefix):
            return "postgresql://" + url[len(prefix) :]
    if url.startswith("postgres://"):
        return "postgresql://" + url[len("postgres://") :]
    return url


#: Seconds libpq may spend opening a connection before it gives up.
#:
#: Set because psycopg's async connect does not fail fast when there is nothing
#: listening -- it hangs. Measured on Windows under `WindowsSelectorEventLoopPolicy`
#: (which `app.runtime` sets, because psycopg refuses the proactor loop): a TCP
#: connect to 127.0.0.1:5432 is refused in under a millisecond, and
#: `engine.connect()` still never returns. With this set it raises `OperationalError`
#: in a few seconds instead.
#:
#: It matters beyond the local machine. `pool_pre_ping` opens a replacement
#: connection when it finds a dead one, so an unreachable database turns every
#: request into a hang rather than an error -- and a request that hangs never
#: reaches the healthcheck's notice, while one that fails does.
CONNECT_TIMEOUT_SECONDS = 10


@lru_cache(maxsize=1)
def get_engine() -> AsyncEngine:
    settings = get_settings()
    return create_async_engine(
        sqlalchemy_url(settings.database_url),
        connect_args={"connect_timeout": CONNECT_TIMEOUT_SECONDS},
        pool_pre_ping=True,
        # Railway restarts the database for maintenance and the pool keeps the dead
        # connections. pre_ping catches that; recycling bounds how long a stale one
        # can sit there in the first place.
        pool_recycle=1_800,
        pool_size=5,
        max_overflow=5,
        echo=False,
    )


@lru_cache(maxsize=1)
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_engine(), expire_on_commit=False, autoflush=False)


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """A transaction that commits on success and rolls back on anything else."""
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency form of the same thing."""
    async with session_scope() as session:
        yield session
