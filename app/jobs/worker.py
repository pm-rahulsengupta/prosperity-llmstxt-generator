"""Worker entrypoint: `python -m app.jobs.worker`.

One process, both queues. Concurrency is deliberately low: a crawl job holds up to
`MAX_BROWSER_CONCURRENCY` Playwright sessions at 800MB apiece, and two concurrent
crawl jobs on a 2GB Railway worker is how you meet the OOM killer. The parallelism
that matters is inside a job -- `fetch_many` runs pages concurrently under its own
semaphores -- not across jobs.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal

from app.config import get_settings
from app.runtime import configure_event_loop

configure_event_loop()

from app.jobs.queue import QUEUE_CRAWL, QUEUE_DEFAULT, app  # noqa: E402

logger = logging.getLogger(__name__)


async def serve_health(port: int) -> None:
    """A liveness endpoint, so Railway can tell a wedged worker from a busy one.

    `railway.json` is shared by every service in the repo, so the healthcheck it
    declares applies to the worker too -- and a worker with no HTTP server fails
    that check and never goes healthy. Answering it is the better resolution than
    removing it: a worker whose event loop has stopped is exactly the condition
    worth restarting, and without a check nothing notices.

    Deliberately thin. It reports that the process is alive, not that the queue is
    healthy; a check that queries Postgres would fail the whole service during a
    database blip that the worker would otherwise ride out.
    """
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route
    from uvicorn import Config, Server

    async def healthz(_request):
        return PlainTextResponse("ok")

    health_app = Starlette(routes=[Route("/healthz", healthz)])
    config = Config(health_app, host="0.0.0.0", port=port, log_level="warning", access_log=False)
    await Server(config).serve()


async def main() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    )
    logger.info(
        "worker starting (llm=%s firecrawl=%s browsers=%d)",
        settings.llm_enabled,
        settings.firecrawl_enabled,
        settings.max_browser_concurrency,
    )

    async with app.open_async():
        stopping = asyncio.Event()

        def request_stop(*_: object) -> None:
            logger.info("shutdown requested; finishing the current job")
            stopping.set()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, request_stop)
            except NotImplementedError:
                # Windows has no signal handlers on the proactor loop. Local dev
                # falls back to KeyboardInterrupt, which is fine; Railway is Linux.
                signal.signal(sig, request_stop)

        worker = asyncio.create_task(
            app.run_worker_async(queues=[QUEUE_DEFAULT, QUEUE_CRAWL], concurrency=1)
        )
        health = asyncio.create_task(serve_health(settings.port))

        await stopping.wait()
        for task in (worker, health):
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.gather(worker, health, return_exceptions=True)


if __name__ == "__main__":
    # Ctrl-C during local development is a normal way to stop the worker, not a
    # crash worth a traceback.
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
