"""HTML to PDF, in the web process, one at a time.

**Chromium is already in the image.** The base is
`mcr.microsoft.com/playwright/python:v1.60.0-noble` and the runtime stage runs
`scrapling install`, which is `playwright install chromium` plus its system deps
into a world-readable `/ms-playwright`. Nothing new is installed and no PDF
library is needed: this is `page.pdf()`, which is Chromium's own print path, so
it renders the same `@media print` rules a client's browser would.

That is also why the print stylesheet came first and this came second. The client
page prints correctly on its own, so if the browser will not start, the feature
degrades to *Print -> Save as PDF* and loses one click rather than the document.

**Why the web process and not the worker.** Two reasons, neither about memory:

* The worker runs `run_worker_async(queues=[default, crawl], concurrency=1)` --
  one job at a time in total. Putting a render on the empty `default` queue does
  not help; a "Download PDF" click would still queue behind a multi-minute crawl.
* There is nowhere to put the bytes. No `LargeBinary` column exists, Railway
  gives the container no persistent filesystem, and `Artifact.body` is a `str`.
  Deferring would mean a binary column or base64-in-text, plus a polling UI, plus
  a retention job -- a lot of machinery bolted onto an app whose every existing
  download is a synchronous response.

The 800MB figure in `app/scrape/fetch.py` is a patchright stealth session against
a hostile third-party site: full JS, images, fingerprint evasion. This is one
headless Chromium loading one same-origin static page with no scripts and six
local fonts. It is still a browser, so it is bounded by a semaphore, a slot
timeout and a render timeout, and it can be switched off entirely with
`PDF_ENABLED=false`.
"""

from __future__ import annotations

import asyncio
import logging
from html import escape

from app.config import get_settings
from app.scrape.fetch import BROWSER_FLAGS

logger = logging.getLogger(__name__)

__all__ = ["PDF_MARGINS", "PdfBusy", "PdfFailed", "PdfUnavailable", "probe_chromium", "render_pdf"]

#: Module level, so it is process-global. `PageFetcher` holds its browser
#: semaphore on the *instance*, which is right there -- a fetcher is one crawl --
#: and exactly wrong here, where a per-request guard would be no guard at all.
#: Nothing in this module may construct a second one.
_SLOT = asyncio.Semaphore(1)

#: Queue politely, then refuse. Forty requests each waiting to hold a browser is
#: worse for everyone than thirty-nine being told to try again.
SLOT_WAIT = 20.0
#: The whole operation, launch to bytes.
RENDER_TIMEOUT = 45.0

#: Mirrors the `@page` margin in static/css/main.css. The two cannot be derived
#: from each other -- a browser will not read our Python, and `page.pdf()`
#: ignores the margin half of `@page` -- so this is the one duplicated number in
#: the feature and it is commented on both sides.
PDF_MARGINS = {"top": "18mm", "bottom": "18mm", "left": "16mm", "right": "16mm"}

#: Set once at boot. `None` means never probed, which is not the same as False.
PDF_READY: bool | None = None


class PdfUnavailable(RuntimeError):
    """No Chromium, or it would not start."""


class PdfBusy(RuntimeError):
    """Another export holds the slot."""


class PdfFailed(RuntimeError):
    """It started and did not finish."""


async def probe_chromium() -> bool:
    """Is there a browser? Asked once, at boot, without launching one.

    The base image ships browsers for Playwright 1.60 and the pinned Playwright
    is 1.62, which wants a different revision directory. `scrapling install`
    should have put the right one in `/ms-playwright` at build time -- this turns
    "should" into a log line instead of a 500 on a client's first download.
    """
    global PDF_READY
    try:
        from pathlib import Path

        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            PDF_READY = Path(pw.chromium.executable_path).exists()
    except Exception as exc:
        logger.warning("PDF export off (%s). The print stylesheet still works.", type(exc).__name__)
        PDF_READY = False
    if PDF_READY:
        logger.info("PDF export available.")
    return PDF_READY


async def render_pdf(path: str, *, doc_title: str, footer_left: str) -> bytes:
    """Render one same-origin path to PDF bytes. Never touches disk."""
    if PDF_READY is False:
        raise PdfUnavailable("chromium is not available in this container")

    try:
        await asyncio.wait_for(_SLOT.acquire(), timeout=SLOT_WAIT)
    except TimeoutError:
        raise PdfBusy("another export is running") from None
    try:
        return await asyncio.wait_for(_render(path, doc_title, footer_left), timeout=RENDER_TIMEOUT)
    except TimeoutError as exc:
        raise PdfFailed("the render did not finish in time") from exc
    finally:
        _SLOT.release()


async def _render(path: str, doc_title: str, footer_left: str) -> bytes:
    from playwright.async_api import async_playwright

    settings = get_settings()
    # Loopback, not `settings.app_url`. Same-origin means the stylesheet, the six
    # woff2 files and the logo resolve exactly as they do for a real browser --
    # and it does not depend on APP_URL being right, or on the Railway edge, or
    # on TLS. `page.set_content()` was the alternative and is a trap: it leaves
    # the document at an opaque origin, so font fetches go CORS-mode with
    # `Origin: null`, StaticFiles sends no CORS headers, and the PDF renders in a
    # fallback face **with no error anywhere**. A silent wrong-font failure is
    # worse than a loud one.
    url = f"http://127.0.0.1:{settings.port}{path}"

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(args=list(BROWSER_FLAGS))
        try:
            page = await browser.new_page(viewport={"width": 1120, "height": 1600})
            response = await page.goto(url, wait_until="load", timeout=20_000)
            if response is None or response.status >= 400:
                raise PdfFailed(f"the page returned {getattr(response, 'status', 'nothing')}")

            # Not `networkidle`: the only thing worth waiting for is the fonts,
            # and this waits for exactly those. A report set in a fallback face
            # is the failure nobody notices.
            await page.evaluate("() => document.fonts.ready")
            # `page.pdf()` forces print media anyway. Setting it explicitly means
            # a debugging `page.screenshot()` shows what the PDF will contain.
            await page.emulate_media(media="print")

            return await page.pdf(
                format="A4",
                # Or every emerald pill and the gunmetal cover print white. The
                # other half of this switch is `print-color-adjust: exact` in the
                # stylesheet; either alone looks like a CSS bug.
                print_background=True,
                prefer_css_page_size=False,
                margin=PDF_MARGINS,
                display_header_footer=True,
                header_template=_header(doc_title),
                footer_template=_footer(footer_left),
                # No `timeout=` here: `Page.pdf()` does not take one in Playwright
                # 1.62, and passing it raises a TypeError at the moment of
                # generating the file -- which a fake `_render` could never catch.
                # `RENDER_TIMEOUT` already bounds the whole operation.
            )
        finally:
            # Launched and closed per request on purpose. A long-lived browser in
            # a process that also serves HTTP is a memory leak with a UI.
            await browser.close()


# Header and footer templates render in an isolated context: no page CSS, no web
# fonts, no relative URLs, and `font-size: 0` unless it is set. So the styling is
# inline and the face is a system sans -- Poppins is genuinely unavailable there,
# which is acceptable for 8pt furniture. `escape` is not decorative: `doc_title`
# and `footer_left` carry a client-supplied name.
_RUN = (
    "font-family:Arial,Helvetica,sans-serif;font-size:8pt;color:#5a6266;"
    "width:100%;padding:0 16mm;display:flex;justify-content:space-between;"
)


def _header(title: str) -> str:
    return f'<div style="{_RUN}"><span>{escape(title)}</span><span></span></div>'


def _footer(left: str) -> str:
    return (
        f'<div style="{_RUN}"><span>{escape(left)}</span>'
        '<span>Page <span class="pageNumber"></span> of '
        '<span class="totalPages"></span></span></div>'
    )
