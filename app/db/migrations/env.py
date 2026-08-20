"""Alembic environment.

Runs synchronously against `sqlalchemy_url(DATABASE_URL)`. Migrations are a one-shot
startup step, not part of the request path, and a sync driver keeps the failure
modes obvious -- an async engine here would add a running event loop to a process
whose only job is to apply DDL and exit.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.config import get_settings
from app.db.base import Base, sqlalchemy_url

# Imported for the side effect of registering every table on Base.metadata.
from app.db import models  # noqa: F401  isort:skip

config = context.config
target_metadata = Base.metadata
config.set_main_option("sqlalchemy.url", sqlalchemy_url(get_settings().database_url))


def include_object(object_, name, type_, reflected, compare_to) -> bool:
    """Keep procrastinate's schema out of autogenerate's sight.

    Its tables are created by revision a1c4f9d2e701 from pinned SQL and are not on
    `Base.metadata`, so autogenerate reads them as tables the models no longer want
    and emits `DROP TABLE procrastinate_jobs`. Running that would delete the job
    queue, including anything queued at the time, and the only warning would be a
    line in a generated file nobody diffed.
    """
    # Substring, not prefix: the queue's own indexes are named both
    # `procrastinate_jobs_lock_idx_v1` and `idx_procrastinate_jobs_worker_not_null`,
    # and a prefix match silently lets the second family through.
    return not (type_ in {"table", "index"} and name and "procrastinate" in name)


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            include_object=include_object,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
