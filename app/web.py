"""Web entrypoint: `python -m app.web`.

A launcher rather than `uvicorn app.main:app` because the event-loop policy must be
set before uvicorn creates its loop, and by the time uvicorn imports the ASGI app
the loop already exists. See `app/runtime.py`.
"""

from __future__ import annotations

from app.runtime import configure_event_loop

configure_event_loop()


def main() -> None:
    import asyncio

    import uvicorn

    from app.config import get_settings

    settings = get_settings()
    config = uvicorn.Config(
        "app.main:app",
        host="0.0.0.0",
        port=settings.port,
        log_level=settings.log_level.lower(),
        # Railway terminates TLS at its edge and forwards the original scheme and
        # client IP in X-Forwarded-*. Without this the OAuth redirect_uri is built
        # as http:// and Google rejects it.
        proxy_headers=True,
        forwarded_allow_ips="*",
        access_log=True,
    )
    # `uvicorn.run()` supplies its own loop factory, which on Windows is the
    # Proactor loop -- overriding the policy set above and putting psycopg back
    # where it cannot connect. Driving the server through `asyncio.run` keeps the
    # loop we chose. On Linux the two paths are equivalent.
    asyncio.run(uvicorn.Server(config).serve())


if __name__ == "__main__":
    main()
