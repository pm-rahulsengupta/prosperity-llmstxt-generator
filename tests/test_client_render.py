"""The client templates, asserted on shape rather than on wording.

The four staff pages carry a Refresh form that POSTs to an arbitrary-host
server-side fetch, a Mark-as-done form, an auth-gated download link and an
htmx-loaded chat panel. None may appear in a client render. Testing that with a
list of guessed strings would pass the day any of them is reworded, so these
assert on structure -- no forms, no scripts, no htmx attributes, no internal
hrefs -- which survives rewording and covers all four in a few lines.
"""

from __future__ import annotations

import re

import pytest
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from lxml import html as lxml_html

from app.core.client_report import SECTION_KEYS, build_client_report
from app.core.components import ComponentState, Family
from app.main import ROOT, _asset_version
from tests.test_client_report import OPERATOR, FakeView, _reports, _status, _taint

# Anything a client following it would hit a sign-in page for, or should never
# learn exists.
STAFF_PREFIXES = (
    "/sites/",
    "/runs/",
    "/agents",
    "/admin",
    "/logout",
    "/login",
    "/clients",
    "/imports",
)


@pytest.fixture(scope="module")
def env() -> Environment:
    environment = Environment(
        loader=FileSystemLoader("templates"), undefined=StrictUndefined, autoescape=True
    )
    environment.globals["asset_version"] = _asset_version
    return environment


def render(env: Environment, section: str = "report", *, status=None, downloads: str = "") -> str:
    report = build_client_report(
        FakeView(), status or _status(), section, client_name="Example Ltd", reports=_reports()
    )
    return env.get_template("client/report.html").render(report=report, downloads=downloads)


def test_every_section_renders(env):
    for section in SECTION_KEYS:
        assert render(env, section).strip().startswith("<!doctype html>")


def test_a_client_render_contains_no_control_a_client_cannot_use(env):
    """Four staff affordances, one assertion.

    Covers the Refresh form, the Mark-as-done form, the refine chat panel and any
    stray button, and keeps covering them when they are reworded.
    """
    doc = lxml_html.fromstring(render(env))

    assert not doc.xpath("//form | //button | //input | //textarea | //select")


def test_a_client_render_runs_no_script(env):
    """Also what lets the share response send a CSP with no script-src at all."""
    doc = lxml_html.fromstring(render(env))

    assert not doc.xpath("//script")
    assert not doc.xpath("//*[@*[starts-with(name(), 'hx-')]]")
    assert not doc.xpath("//*[@*[starts-with(name(), 'on')]]"), "no inline event handlers"


def test_no_link_points_anywhere_a_client_cannot_go(env):
    doc = lxml_html.fromstring(render(env))

    leaked = [
        href
        for href in doc.xpath("//@href")
        if any(href.startswith(prefix) for prefix in STAFF_PREFIXES)
    ]

    assert leaked == [], f"staff URLs in a client render: {leaked}"


def test_no_operator_email_survives_a_render(env):
    """The leak the whole view model exists to close, asserted at the far end."""
    assert OPERATOR not in render(env)


def test_no_tainted_field_survives_a_render(env):
    """The taint round-trip, carried through Jinja rather than stopping at repr.

    Every section except the handover, which carries `verify` on purpose for the
    client's own developer. Asserting against `report` here would fail on that
    deliberate crossing and prove nothing about the rest.
    """
    for section in ("overview", "checklist", *[f.value for f in Family]):
        status = _status()
        sentinels = _taint(status)

        html = render(env, section, status=status)

        leaked = sorted(name for name, token in sentinels.items() if token in html)
        assert leaked == [], f"{section} leaked: {leaked}"


def test_the_handover_carries_verify_and_only_verify(env):
    """The one deliberate crossing, pinned so it stays deliberate."""
    status = _status()
    sentinels = _taint(status)

    html = render(env, "handover", status=status)

    leaked = sorted(name for name, token in sentinels.items() if token in html)
    assert leaked and all(name.startswith("verify:") for name in leaked), (
        f"the handover leaked something other than verify: {leaked}"
    )


def test_no_rule_identifier_survives_a_render(env):
    from app.core.rules.agents_rules import AGENTS_RULES
    from app.core.rules.crawl_rules import CRAWL_RULES
    from app.core.rules.delivery_rules import CATALOG_RULES, HEADER_RULES
    from app.core.rules.full_rules import FULL_RULES
    from app.core.rules.index_rules import INDEX_RULES

    every = [*INDEX_RULES, *FULL_RULES, *AGENTS_RULES, *CRAWL_RULES, *HEADER_RULES, *CATALOG_RULES]
    pattern = re.compile(rf"\b({'|'.join(sorted({r.id.split('-')[0] for r in every}))})-\d+\b")

    assert pattern.search(render(env)) is None


def test_a_crawled_page_title_containing_markup_is_escaped(env):
    """The share page renders strings sourced from a third-party website to an
    unauthenticated audience. Autoescaping is what stops that being an XSS; this
    is the test that says so out loud."""
    status = _status()
    crawl = next(s for s in status.statuses if s.component.family is Family.CRAWL)
    crawl.detail = "<script>alert(1)</script>"
    crawl.probe_decided = True
    crawl.state = ComponentState.LIVE

    html = render(env, "crawl", status=status)

    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_the_document_says_who_prepared_it_and_for_whom(env):
    html = render(env)

    assert "Prosperity Media" in html
    assert "Example Ltd" in html


def test_a_stale_report_says_so(env):
    """FakeView is stale. A client reading month-old findings should be told."""
    assert "more than a day old" in render(env)


def test_downloads_appear_only_when_a_route_is_supplied(env):
    """A staff preview has no share token, so it offers no file links.

    The client's copy gets them through `/share/{token}/download/...`; there is
    no other URL that would work for someone not signed in.
    """
    without = lxml_html.fromstring(render(env, "checklist"))
    with_links = lxml_html.fromstring(render(env, "checklist", downloads="/share/abc/download"))

    assert not without.xpath("//a[contains(@href, '/download')]")
    assert with_links.xpath("//a[contains(@href, '/share/abc/download')]")


def test_the_combined_report_lists_its_contents(env):
    doc = lxml_html.fromstring(render(env, "report"))

    assert doc.xpath("//nav[@aria-label='Contents']//a")


def test_a_single_section_has_no_contents_list(env):
    """One entry is not a table of contents."""
    doc = lxml_html.fromstring(render(env, "crawl"))

    assert not doc.xpath("//nav[@aria-label='Contents']")


def test_a_filename_is_rendered_exactly_as_it_will_be_saved(env):
    """A global `th` rule uppercases column headings, and a `<th scope="row">`
    inherits it -- which rendered `llms.txt` as `LLMS.TXT`, and family names as
    "CRAWL RULES". A client copying a filename off a PDF has to get the filename.

    Asserted against the stylesheet, not the markup. The markup always carries
    the correct text; the uppercasing happened in CSS, so a test that parses the
    HTML would pass while the page was wrong -- which is what the first version
    of this test did.
    """
    css = (ROOT / "static" / "css" / "main.css").read_text(encoding="utf-8")

    block = css.split(".c-files tbody th,", 1)
    assert len(block) == 2, "the row-header override is gone"
    rule = block[1].split("}", 1)[0]

    assert "text-transform: none" in rule
    assert ".c-table tbody th" in block[1][: len(rule) + 40], "the overview table needs it too"
