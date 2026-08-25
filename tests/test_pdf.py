"""PDF export.

The render itself needs a browser, so those tests skip without one. The bounding
-- the semaphore, the slot timeout, the refusal path -- does not, and that is the
part most likely to be quietly loosened later, so it is tested with a fake render
rather than a real Chromium.
"""

from __future__ import annotations

import asyncio
import sys

import pytest

from app import pdf

# No `pytestmark`: `asyncio_mode = "auto"` already runs the async tests, and a
# module-level asyncio mark makes pytest warn on every sync test here.


@pytest.fixture(autouse=True)
def fresh_slot(monkeypatch):
    """A semaphore per test.

    `_SLOT` is module-level, which is right in production -- the web process has
    one event loop and one browser budget -- but an `asyncio.Semaphore` binds to
    the loop that first awaits it, and pytest gives each test its own loop. Left
    shared, the second async test in this file fails with "bound to a different
    event loop", which says nothing about the code under test.
    """
    monkeypatch.setattr(pdf, "_SLOT", asyncio.Semaphore(1))


def _own_loop(coro_factory):
    """Run one coroutine on a loop this function owns.

    Playwright starts its driver as a subprocess. On Windows that needs a
    Proactor loop, and pytest-asyncio hands out a Selector one, where
    `subprocess_exec` raises `NotImplementedError` -- so a Playwright test run
    through pytest-asyncio fails on Windows for a reason that has nothing to do
    with the code. This builds the right loop and runs the coroutine on it, so
    the test exercises Chromium on both platforms instead of skipping on one.

    Only the real-render test needs this. Everything else here fakes `_render`
    and never launches anything.
    """
    import asyncio

    # The policy is process-global and pytest-asyncio builds every other test's
    # loop from it, so it is restored. Leaving it changed made thirteen unrelated
    # tests fail and the suite take three minutes instead of thirty seconds --
    # the kind of breakage that gets blamed on whatever ran next.
    previous = asyncio.get_event_loop_policy()
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro_factory())
    finally:
        loop.close()
        asyncio.set_event_loop_policy(previous)


# -- bounding, which needs no browser -----------------------------------------


async def test_two_renders_do_not_run_at_once(monkeypatch):
    """The semaphore is module-level on purpose.

    `PageFetcher` holds its browser semaphore on the *instance*, which is right
    there -- a fetcher is one crawl -- and would be exactly wrong here, where a
    per-request guard is no guard at all.
    """
    concurrent = 0
    peak = 0

    async def fake_render(path, title, footer):
        nonlocal concurrent, peak
        concurrent += 1
        peak = max(peak, concurrent)
        await asyncio.sleep(0.05)
        concurrent -= 1
        return b"%PDF-fake"

    monkeypatch.setattr(pdf, "_render", fake_render)
    monkeypatch.setattr(pdf, "PDF_READY", True)

    await asyncio.gather(
        *[pdf.render_pdf("/share/x", doc_title="t", footer_left="f") for _ in range(4)]
    )

    assert peak == 1, f"{peak} renders ran at once"


async def test_a_render_that_waits_too_long_is_refused(monkeypatch):
    """Refusing beats queueing forty requests each waiting to hold a browser."""

    async def slow_render(path, title, footer):
        await asyncio.sleep(5)
        return b"%PDF-fake"

    monkeypatch.setattr(pdf, "_render", slow_render)
    monkeypatch.setattr(pdf, "PDF_READY", True)
    monkeypatch.setattr(pdf, "SLOT_WAIT", 0.05)

    first = asyncio.create_task(pdf.render_pdf("/a", doc_title="t", footer_left="f"))
    await asyncio.sleep(0.01)

    with pytest.raises(pdf.PdfBusy):
        await pdf.render_pdf("/b", doc_title="t", footer_left="f")

    first.cancel()


async def test_a_render_that_hangs_is_cut_off(monkeypatch):
    async def hanging(path, title, footer):
        await asyncio.sleep(10)

    monkeypatch.setattr(pdf, "_render", hanging)
    monkeypatch.setattr(pdf, "PDF_READY", True)
    monkeypatch.setattr(pdf, "RENDER_TIMEOUT", 0.05)

    with pytest.raises(pdf.PdfFailed):
        await pdf.render_pdf("/a", doc_title="t", footer_left="f")


async def test_the_slot_is_released_even_when_a_render_fails(monkeypatch):
    """A leaked slot would make the second export fail forever, and look like a
    browser problem."""

    async def exploding(path, title, footer):
        raise RuntimeError("boom")

    monkeypatch.setattr(pdf, "_render", exploding)
    monkeypatch.setattr(pdf, "PDF_READY", True)

    with pytest.raises(RuntimeError):
        await pdf.render_pdf("/a", doc_title="t", footer_left="f")

    assert not pdf._SLOT.locked()


async def test_no_browser_is_refused_rather_than_attempted(monkeypatch):
    monkeypatch.setattr(pdf, "PDF_READY", False)

    with pytest.raises(pdf.PdfUnavailable):
        await pdf.render_pdf("/a", doc_title="t", footer_left="f")


async def test_never_probed_is_not_the_same_as_no_browser(monkeypatch):
    """`PDF_READY = None` means nobody asked. Treating it as False would turn a
    missed boot probe into a permanently broken feature."""
    monkeypatch.setattr(pdf, "PDF_READY", None)

    async def fake_render(path, title, footer):
        return b"%PDF-fake"

    monkeypatch.setattr(pdf, "_render", fake_render)

    assert await pdf.render_pdf("/a", doc_title="t", footer_left="f") == b"%PDF-fake"


# -- the running head ----------------------------------------------------------


def test_a_client_name_cannot_break_out_of_the_header_template():
    """The header renders as raw HTML in an isolated context, and the name is
    client-supplied."""
    header = pdf._header('Acme <img src=x onerror="alert(1)">')

    assert "<img" not in header
    assert "&lt;img" in header


def test_the_footer_numbers_the_pages():
    """Chromium does not implement CSS margin boxes, so `counter(page)` is not
    available -- the page number can only come from these two spans."""
    footer = pdf._footer("Prepared by Prosperity Media")

    assert 'class="pageNumber"' in footer
    assert 'class="totalPages"' in footer


def test_the_margins_match_the_stylesheet():
    """`page.pdf()` ignores the margin half of `@page`, so the two are duplicated
    on purpose. This is what stops them drifting."""
    from pathlib import Path

    css = (Path(__file__).resolve().parents[1] / "static" / "css" / "main.css").read_text(
        encoding="utf-8"
    )
    page_rule = css.split("@page {", 1)[1].split("}", 1)[0]

    assert pdf.PDF_MARGINS["top"] in page_rule
    assert pdf.PDF_MARGINS["left"] in page_rule


# -- an actual render ----------------------------------------------------------


def test_a_real_render_produces_a_pdf(monkeypatch, tmp_path):
    """End to end through Chromium, against a page served over loopback.

    The one thing a fake cannot tell us: that `page.pdf()` runs at all here, and
    that what comes back is a PDF.
    """
    import http.server
    import shutil
    import threading
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    shutil.copytree(root / "static", tmp_path / "static")
    (tmp_path / "index.html").write_text(
        '<!doctype html><html><head><meta charset="utf-8">'
        '<link rel="stylesheet" href="/static/css/main.css">'
        "</head><body class='client-doc'><div class='c-doc'>"
        "<h1>Readiness</h1><p>One page.</p></div></body></html>",
        encoding="utf-8",
    )

    handler = type(
        "Quiet",
        (http.server.SimpleHTTPRequestHandler,),
        {
            "log_message": lambda *a, **k: None,
            "__init__": lambda self, *a, **k: http.server.SimpleHTTPRequestHandler.__init__(
                self, *a, directory=str(tmp_path), **k
            ),
        },
    )
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(
        pdf, "get_settings", lambda: type("S", (), {"port": server.server_address[1]})()
    )
    monkeypatch.setattr(pdf, "PDF_READY", True)

    async def render():
        if not await pdf.probe_chromium():
            return None
        return await pdf.render_pdf("/index.html", doc_title="Readiness", footer_left="Prosperity")

    try:
        body = _own_loop(render)
    finally:
        server.shutdown()

    if body is None:
        pytest.skip("no Chromium in this environment")

    assert body.startswith(b"%PDF-"), body[:20]
    assert len(body) > 1000, "a PDF that small is probably an error page"
