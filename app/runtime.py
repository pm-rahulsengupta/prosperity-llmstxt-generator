"""Process-level setup shared by the web and worker entrypoints.

The only thing in here is the Windows event-loop policy, and it exists because of
a platform split that is invisible until you try to run the worker locally:

Python 3.8+ on Windows defaults to `ProactorEventLoop`, and psycopg 3 refuses to
run in async mode on it -- "Psycopg cannot use the 'ProactorEventLoop'". Every
connection attempt fails, forever, at one warning per retry. Railway is Linux, so
this never appears in deploy; it appears the moment a developer on this Windows
machine runs `python -m app.jobs.worker`, which is exactly when it is least
welcome.

Setting the policy has to happen before the loop is created, which is why both
entrypoints are launcher modules rather than `uvicorn app.main:app` and a bare
procrastinate CLI call. Nothing else in the app may create a loop before this runs.
"""

from __future__ import annotations

import asyncio
import sys


def configure_event_loop() -> None:
    """Make asyncio usable with psycopg on Windows. A no-op everywhere else."""
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
