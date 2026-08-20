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
        await stopping.wait()
        worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker


if __name__ == "__main__":
    # Ctrl-C during local development is a normal way to stop the worker, not a
    # crash worth a traceback.
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
