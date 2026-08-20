"""The procrastinate app.

Postgres is already a dependency, so the queue lives in it rather than adding Redis
for the sake of four job types. procrastinate uses LISTEN/NOTIFY, so a queued job
starts immediately instead of waiting out a poll interval.

Why a queue at all: the source ran a 1,500-URL crawl as one synchronous POST behind
a load balancer, which is a guaranteed gateway timeout and, worse, a run whose
progress nobody can see. Everything slow happens here, and the web service only ever
enqueues and reads.
"""

from __future__ import annotations

from functools import lru_cache

from procrastinate import App, PsycopgConnector

from app.config import get_settings
from app.db.base import psycopg_dsn

QUEUE_DEFAULT = "default"
# Crawls hold a browser slot for minutes at a time. Keeping them off the default
# queue means a two-second assemble job is never stuck behind one.
QUEUE_CRAWL = "crawl"


@lru_cache(maxsize=1)
def get_app() -> App:
    settings = get_settings()
    return App(
        connector=PsycopgConnector(conninfo=psycopg_dsn(settings.database_url)),
        import_paths=["app.jobs.tasks"],
    )


app = get_app()
