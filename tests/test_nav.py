"""The left-hand navigation.

It renders on every page, which makes it the one change in the agents.md work
that can break screens unrelated to the feature. Most of these are therefore
about the chrome not falling over rather than about the links themselves.
"""

from __future__ import annotations

import pytest

from app.nav import build_nav


def flat(groups):
    return {item.title: item for group in groups for item in group.items}


def test_no_nav_item_is_named_after_a_file_it_does_not_open():
    """The old sidebar had a "Generate" group offering "llms.txt" and "agents.md".

    Neither opened a file. `llms.txt` was the crawl-run starter and `agents.md`
    was the check-a-site form -- two processes named after two files that also
    exist as components in the Content and Agent-instructions families, two
    groups further down. The same name meant two things on one screen.
    """
    from app.core.components import COMPONENTS

    artifacts = {c.artifact for c in COMPONENTS if c.artifact}
    named_after_a_file = {t for t in flat(build_nav("/", "x.example")) if t in artifacts}

    assert named_after_a_file == set(), named_after_a_file


def test_the_crawl_runner_is_named_for_what_it_does():
    items = flat(build_nav("/"))

    assert "Crawl runs" in items
    assert "Check any site" in items


def test_the_index_is_active_only_on_the_index():
    """`/` is a prefix of every path and would otherwise light everywhere."""
    assert flat(build_nav("/"))["Crawl runs"].active
    assert not flat(build_nav("/agents"))["Crawl runs"].active
    assert not flat(build_nav("/admin"))["Crawl runs"].active


def test_a_run_page_lights_the_section_it_belongs_to():
    """A run is the output of the crawl flow.

    Leaving the sidebar entirely dark on the page an operator spends most of
    their time on is a worse answer than naming its section.
    """
    assert flat(build_nav("/runs/abc-123"))["Crawl runs"].active


def test_the_check_form_lights_on_its_own_page():
    items = flat(build_nav("/agents"))
    assert items["Check any site"].active
    assert not items["Crawl runs"].active


# -- gap counts ---------------------------------------------------------------


def test_a_family_with_outstanding_work_carries_its_count():
    from app.core.components import Family

    items = flat(build_nav("/", "x.example", gaps={Family.CRAWL: 2}))

    assert items["Crawl rules"].gap == 2


def test_measured_and_clear_is_zero_not_absent():
    """`0` and `None` are different answers and the nav must not merge them.

    Zero means every applicable component in that family is live. `None` means
    nothing has been measured -- no client, or no stored probe. A family with no
    data must not read as a family with no problems.
    """
    from app.core.components import Family

    measured = flat(build_nav("/", "x.example", gaps={Family.CONTENT: 0}))
    unmeasured = flat(build_nav("/", "x.example"))

    assert measured["Content"].gap == 0
    assert unmeasured["Content"].gap is None


def test_an_unmeasured_family_renders_no_pill_rather_than_a_zero():
    html = _render("clients.html", rows=[], deleted="", domain="x.example")

    assert 'class="gap"' not in html


def test_a_measured_family_renders_its_count():
    from app.core.components import Family

    html = _render(
        "clients.html",
        rows=[],
        deleted="",
        domain="x.example",
        nav_gaps={Family.CRAWL: 3, Family.CONTENT: 0},
    )

    assert ">3<" in html
    assert "gap done" in html, "zero renders a tick, not an empty space"


def test_site_items_are_shown_but_marked_when_there_is_no_domain():
    """GEO Tracker keeps its Team page visible and lets the page explain itself.

    Hiding an item leaves an operator hunting for a page that exists.
    """
    brief = flat(build_nav("/"))["Brief"]

    assert brief.disabled
    assert brief.hint
    assert brief.url == "/clients", "with no domain it points at the picker"


def test_the_picker_it_points_at_actually_exists():
    """This assertion used to encode a fiction.

    `nav.py` said disabled links "point at the picker rather than at a dead
    path", and the test asserted they pointed at `/`. There was no picker: `/`
    was the run starter, and the only way to reach a client was to find one of
    their runs in the most recent forty and click it.
    """
    from app.main import app

    paths = {getattr(route, "path", "") for route in app.routes}

    assert "/clients" in paths
    assert flat(build_nav("/"))["Brief"].url in paths


def test_site_items_become_usable_once_a_domain_is_known():
    brief = flat(build_nav("/", domain="example.com"))["Brief"]

    assert not brief.disabled
    assert brief.url == "/sites/example.com/brief"


def test_admin_items_are_hidden_from_a_non_admin():
    """`require_admin_or_404` already hides the pages; this hides the doors."""
    assert "Costs" not in flat(build_nav("/", is_admin=False))
    assert "Costs" in flat(build_nav("/", is_admin=True))


@pytest.mark.parametrize(
    "path", ["/", "/agents", "/runs/x", "/admin", "/admin/runs", "/accounts", "/login", "/nope"]
)
def test_at_most_one_item_is_active_on_any_page(path):
    """Two lit items is worse than none: it says the tool does not know where
    the operator is."""
    active = [i for i in flat(build_nav(path, "example.com", True)).values() if i.active]
    assert len(active) <= 1, [i.title for i in active]


def test_every_item_has_a_destination():
    for group in build_nav("/", "example.com", True):
        for item in group.items:
            assert item.url.startswith("/"), item


# -- the chrome renders on every template ------------------------------------


@pytest.mark.parametrize(
    "template",
    ["index.html", "brief.html", "login.html", "signup.html", "accounts.html"],
)
def test_the_sidebar_does_not_break_existing_pages(template):
    """The real risk in adding chrome: a 500 on a page unrelated to the feature.

    Rendered through the app's own environment, since `base.html` reads globals
    registered on it, with `StrictUndefined` so a variable the sidebar expects and
    a route does not pass raises here rather than in production.
    """
    from types import SimpleNamespace

    from jinja2 import StrictUndefined

    from app.core.onboarding import QUESTIONS, SiteBrief
    from app.main import _brief_form_values, templates

    env = templates.env
    previous, env.undefined = env.undefined, StrictUndefined
    try:
        html = env.get_template(template).render(
            request=SimpleNamespace(url=SimpleNamespace(path="/"), query_params={}),
            user=SimpleNamespace(email="a@b.c", is_admin=True),
            runs=[],
            domain="example.com",
            questions=QUESTIONS,
            answers=_brief_form_values(SiteBrief()),
            run_id=None,
            drift_reason=None,
            metrics={},
            imported=None,
            import_notes=None,
            gsc_enabled=False,
            suggested=[],
            reasoning="",
            llm_used=False,
            dropped=[],
            readiness=None,
            users=[],
            accounts=[],
            error=None,
            next_url="/",
        )
    finally:
        env.undefined = previous

    assert 'class="side"' in html
    assert "Check any site" in html


# -- the eight new pages ------------------------------------------------------


def _render(template: str, **extra):
    """Render through the app's own environment with StrictUndefined.

    `base.html` reads globals registered on that environment, so a fresh one
    would test a template that does not exist. Strict undefined turns a context
    key a route forgets into a failure here instead of a 500 in production.
    """
    from jinja2 import StrictUndefined

    from app.main import templates as app_templates

    context = _render_context(**extra)
    env = app_templates.env
    previous, env.undefined = env.undefined, StrictUndefined
    try:
        return env.get_template(template).render(**context)
    finally:
        env.undefined = previous


def _render_context(**extra) -> dict:
    """The context `_render` hands a template.

    Split out so `test_the_render_fixture_supplies_every_key_the_real_context
    _builds` can compare it against what `_component_context` actually returns,
    rather than the two drifting until nine templates fail at once.
    """
    from types import SimpleNamespace

    from app.core.components import SiteType
    from app.core.site_state import derive
    from app.core.templates_lib import build_templates

    status = derive(
        "https://x.example",
        SiteType.CONTENT,
        artifacts={"llms.txt": "# x", "robots.txt": "# r"},
        templates=build_templates("https://x.example"),
    )
    context = {
        "request": SimpleNamespace(url=SimpleNamespace(path="/"), query_params={}),
        "user": SimpleNamespace(email="a@b.c", is_admin=True),
        "domain": "x.example",
        "site_url": "https://x.example",
        "site_type": "content",
        "platform": "wordpress",
        "markable": {"cls", "cursor"},
        "statuses": status.for_client(),
        "grouped": status.by_effort(),
        "family_label": "Crawl rules",
        "family_blurb": "Who may crawl this site.",
        "done": 1,
        "total": 5,
        "dev_count": 12,
        "checked_ago": "3 hours ago",
        "is_stale": False,
        "label": "",
        "reports": {},
        "refinable": False,
        "judged": __import__("app.core.evidence", fromlist=["JUDGED_BY"]).JUDGED_BY,
        "share_enabled": True,
        "sections": ["report", "checklist", "handover"],
        "default_days": 30,
        "max_days": 90,
        "minted": "",
        "error": "",
        "share_section": "report",
        "family_key": "crawl",
        # Overridden by the tests that assert on the sidebar's gap pills.
        "nav_gaps": {},
        # From `_settings_context`, which the settings page uses instead.
        "links": [],
        "now": __import__("datetime").datetime.now(__import__("datetime").UTC),
        "exists": True,
        "going": None,
        **extra,
    }
    return context


@pytest.mark.parametrize("template", ["family.html", "checklist.html", "handover.html"])
def test_the_new_pages_render(template):
    html = _render(template)

    assert 'class="side"' in html
    assert "Your checklist" in html


def test_the_family_page_shows_a_components_state_and_how_to_verify_it():
    html = _render("family.html")

    assert "Verify:" in html
    assert "robots.txt" in html


def test_a_template_is_rendered_with_its_warning_and_no_download():
    """The one thing that must never slip: a placeholder offered as a file."""
    from app.core.components import SiteType
    from app.core.site_state import derive
    from app.core.templates_lib import build_templates

    status = derive(
        "https://x.example", SiteType.APP_API, templates=build_templates("https://x.example")
    )
    card = status.by_key("mcp-card")
    html = _render("family.html", statuses=[card], markable=set())

    assert "Not for publication" in html
    assert "REPLACE_ME" in html
    assert "Download" not in html


def test_a_ready_artefact_offers_a_download():
    from app.core.components import SiteType
    from app.core.site_state import derive

    status = derive("https://x.example", SiteType.CONTENT, artifacts={"llms.txt": "# x"})
    html = _render("family.html", statuses=[status.by_key("llms-txt")], markable=set())

    assert "Download llms.txt" in html


def test_only_markable_components_show_a_mark_button():
    from app.core.components import SiteType
    from app.core.site_state import derive

    status = derive("https://x.example", SiteType.CONTENT, artifacts={"llms.txt": "# x"})
    llms = status.by_key("llms-txt")

    assert "Mark as done" not in _render("family.html", statuses=[llms], markable=set())
    assert "Mark as done" in _render("family.html", statuses=[llms], markable={"llms-txt"})


def test_an_empty_family_explains_itself():
    """A page of nothing reads as a broken tool on exactly the sites that need it."""
    html = _render("family.html", statuses=[])

    assert "That is a decision rather than a gap" in html


def test_the_nav_offers_every_family_and_both_action_lists():
    from app.core.components import FAMILY_LABELS

    items = flat(build_nav("/", domain="x.example"))
    for label in FAMILY_LABELS.values():
        assert label in items, label
    assert "Your checklist" in items
    assert "Developer handover" in items


# -- the overview -------------------------------------------------------------


def _readiness(passed: int = 4, of: int = 10):
    """A real report rather than a stand-in.

    A `SimpleNamespace(score=42)` renders the number and then explodes on
    `summary()` -- the method carrying the sample the score came from, which is
    exactly what a page must not omit. Building the real object means the test
    exercises what the template actually calls.

    Takes a pass count rather than a target score, because the score is a
    weighted property and asking for an arbitrary number would mean the helper
    quietly producing something else.
    """
    from app.core.components import SiteType, by_key
    from app.scrape.readiness import CheckResult, CheckState, ReadinessReport

    report = ReadinessReport(site_url="https://x.example", site_type=SiteType.CONTENT)
    report.sampled = ["https://x.example/", "https://x.example/blog/a-post"]
    for n in range(of):
        state = CheckState.PASS if n < passed else CheckState.FAIL
        report.results.append(CheckResult(by_key("llms-txt"), state, f"check {n}"))
    return report


def _overview(**extra):
    """The client overview, rendered."""
    return _render("client_home.html", **{**_overview_context(), **extra})


def _overview_context(**extra):
    """Whatever the route would have passed. One definition, two readers."""
    from app.scrape.agents_probe import ProbeResult, Surface, SurfaceState

    probe = ProbeResult(
        site_url="https://x.example",
        llms_txt=Surface(
            url="https://x.example/llms.txt",
            state=SurfaceState.PRESENT,
            status=200,
            content_type="text/markdown",
        ),
        agents_md=Surface(url="https://x.example/agents.md", state=SurfaceState.ABSENT, status=404),
        notes=["Detected WordPress from a generator meta tag."],
    )
    from types import SimpleNamespace

    from app.core.components import SiteType
    from app.core.site_state import derive
    from app.core.templates_lib import build_templates

    status = derive(
        "https://x.example",
        SiteType.CONTENT,
        artifacts={"llms.txt": "# x", "robots.txt": "# r"},
        templates=build_templates("https://x.example"),
    )
    context = {
        "checked_ago": "3 hours ago",
        "is_stale": False,
        "label": "",
        "onboarded": True,
        "runs": [],
        "mark_count": 2,
        "metrics": {},
        "probe": probe,
        "doc": SimpleNamespace(transactional=False, ucp_version="", site_name="X"),
        "tech": SimpleNamespace(
            platform=SimpleNamespace(value="wordpress"),
            platform_evidence="generator meta tag",
        ),
        "catalog": None,
        "readiness": _readiness(),
        "status": status,
        "family_rows": status.family_counts(),
        "client_count": len(status.for_client()),
        "dev_count": len(status.for_developer()),
        "bundle": None,
        "rendered": "",
        **extra,
    }
    return context


def test_the_check_form_renders_with_nothing_but_a_user():
    """The GET route passes almost nothing; StrictUndefined catches a forgotten key."""
    html = _render("agents.html", site_url="")

    assert "Check the site" in html
    assert 'class="side"' in html


def test_the_client_overview_answers_how_the_site_is_doing_without_opening_a_tab():
    html = _overview()

    assert f"{_readiness().score}/100" in html, "the readiness score"
    assert "wordpress" in html, "the platform"
    assert "llms.txt" in html, "what the site publishes today"
    assert "Your checklist" in html and "Developer handover" in html


def test_every_page_built_on_a_stored_probe_says_how_old_it_is():
    """A cached number shown as a live one is the failure caching introduces.

    Asserted across every such page rather than on one, because the way this
    fails is a new page that forgets the partial -- and it would look correct.
    """
    for template in ("family.html", "checklist.html", "handover.html"):
        assert "Checked 3 hours ago" in _render(template), template

    assert "Checked 3 hours ago" in _overview()


def test_a_stale_snapshot_says_so_rather_than_just_showing_its_age():
    """ "Checked 2 days ago" is a fact. "Refresh before quoting it" is the advice."""
    html = _render("family.html", checked_ago="2 days ago", is_stale=True)

    assert "over a day old" in html
    assert "refresh before quoting it" in html


def test_the_overview_counts_agree_with_the_tabs():
    """Two derivations that disagree would make the overview worse than nothing."""
    from app.core.components import SiteType
    from app.core.site_state import derive
    from app.core.templates_lib import build_templates

    status = derive(
        "https://x.example",
        SiteType.CONTENT,
        artifacts={"llms.txt": "# x", "robots.txt": "# r"},
        templates=build_templates("https://x.example"),
    )
    rows = status.family_counts()

    assert sum(c["total"] for _, _, c in rows) == status.applicable_count
    assert sum(c["live"] for _, _, c in rows) == status.live_count
    for _, _, counts in rows:
        assert (
            counts["live"] + counts["ready"] + counts["template"] + counts["missing"]
            == (counts["total"])
        ), "a component in a state the overview does not count would vanish from the totals"


def test_a_family_with_nothing_applicable_is_left_out_rather_than_shown_as_zero():
    from app.core.components import SiteType
    from app.core.site_state import ComponentState, derive

    status = derive("https://x.example", SiteType.CONTENT)
    for family, _, counts in status.family_counts():
        assert counts["total"] > 0, family
        applicable = [
            s for s in status.family(family) if s.state is not ComponentState.NOT_APPLICABLE
        ]
        assert len(applicable) == counts["total"]


# -- the client pages ---------------------------------------------------------


def test_the_client_list_renders_with_and_without_clients():
    """A first-run instance sees this page before anything else exists."""
    empty = _render("clients.html", rows=[], deleted="")
    assert "No clients yet" in empty
    assert "Add a client" in empty

    filled = _render(
        "clients.html",
        deleted="",
        rows=[
            {
                "domain": "x.example",
                "label": "Big Client",
                "onboarded": True,
                "snapshot": {"score": 53, "checked_ago": "2 hours ago", "is_stale": False},
            },
            {"domain": "y.example", "label": "", "onboarded": False, "snapshot": None},
        ],
    )
    assert "Big Client" in filled
    assert "53/100" in filled
    assert "needs onboarding" in filled


def test_a_client_never_checked_says_so_rather_than_showing_a_zero():
    """`None` is "not checked". A 0/100 would read as a site that failed everything."""
    html = _render(
        "clients.html",
        deleted="",
        rows=[{"domain": "y.example", "label": "", "onboarded": False, "snapshot": None}],
    )

    assert "never checked" in html
    assert "0/100" not in html


def test_the_unchecked_page_offers_a_check_rather_than_running_one():
    html = _render("unchecked.html", title="Your checklist")

    assert "has not been checked yet" in html
    assert "/sites/x.example/refresh" in html
    assert "Checking reads about thirty pages" in html


def test_the_settings_page_states_what_a_delete_would_remove():
    """A confirm screen that says "are you sure" and nothing else is not consent."""
    from app.db.repo import ClientDeletion

    html = _render(
        "client_settings.html",
        exists=True,
        error=None,
        going=ClientDeletion(
            domain="x.example",
            runs=2,
            pages=80,
            marks=3,
            metric_rows=412,
            snapshots=1,
            edits=1,
            spend_rows=0,
            share_links=0,
            config=1,
        ),
    )

    assert "2 runs" in html and "80 crawled pages" in html and "412 search-metric rows" in html
    assert "Type <code>x.example</code> to confirm" in html


def test_the_danger_zone_is_hidden_from_a_non_admin():
    from app.db.repo import ClientDeletion

    nothing = ClientDeletion(
        domain="x.example",
        runs=0,
        pages=0,
        marks=0,
        metric_rows=0,
        snapshots=0,
        edits=0,
        spend_rows=0,
        share_links=0,
        config=1,
    )
    html = _render(
        "client_settings.html",
        exists=True,
        error=None,
        going=nothing,
        user=__import__("types").SimpleNamespace(email="a@b.c", is_admin=False),
    )

    assert "Danger zone" not in html
    assert "Delete this client" not in html


def test_the_add_client_page_renders():
    html = _render("client_new.html", error=None)

    assert "Add a client" in html
    assert 'name="site_url"' in html


@pytest.mark.parametrize(
    "template",
    [
        "clients.html",
        "client_new.html",
        "client_home.html",
        "client_settings.html",
        "unchecked.html",
        "agents.html",
        "family.html",
        "checklist.html",
        "handover.html",
    ],
)
def test_every_page_renders_under_strict_undefined(template):
    """The sweep the plan asked to grow from nine pages to the reshaped set.

    A context key a route forgets fails here rather than as a 500 in production.
    """
    from app.db.repo import ClientDeletion

    html = _render(
        template,
        rows=[],
        deleted="",
        error=None,
        exists=True,
        title="Overview",
        site_url="",
        going=ClientDeletion(
            domain="x.example",
            runs=0,
            pages=0,
            marks=0,
            metric_rows=0,
            snapshots=0,
            edits=0,
            spend_rows=0,
            share_links=0,
            config=1,
        ),
        **(_overview_context() if template == "client_home.html" else {}),
    )

    assert 'class="side"' in html or template in ("login.html", "signup.html")
    assert "<h1" in html, "every page opens at h1"


# -- what the guidelines review found ----------------------------------------


def test_every_page_offers_a_skip_link_before_the_sidebar():
    """Up to fourteen nav links precede the content on every page.

    Below the breakpoint the sidebar becomes a horizontal band, so a keyboard
    user crosses all of them to reach anything.
    """
    html = _render("clients.html", rows=[], deleted="")

    assert '<a class="skip" href="#main">' in html
    assert 'id="main"' in html
    assert html.index('class="skip"') < html.index('class="side"')


def test_a_disabled_nav_item_says_why_to_a_screen_reader():
    """It renders as an ordinary link with a `title`, and a title is mouse-only."""
    # No domain, which is when the site-scoped items are the disabled ones.
    html = _render("clients.html", rows=[], deleted="", domain="")

    assert "visually-hidden" in html
    assert "Pick a client first" in html


def test_email_fields_are_typed_as_email():
    """type=text gives no mobile keyboard hint and no browser validation."""
    for template in ("login.html", "signup.html"):
        html = _render(template, error=None, sso_enabled=False)
        assert 'type="email"' in html, template
        assert 'spellcheck="false"' in html, template


def test_a_domain_field_does_not_invite_a_password_manager():
    html = _render("client_new.html", error=None)

    assert 'autocomplete="off"' in html
    assert 'spellcheck="false"' in html


def test_the_progress_poller_announces_itself():
    """It swaps content every three seconds with no announcement otherwise."""
    from pathlib import Path

    run_html = Path("templates/run.html").read_text(encoding="utf-8")

    assert 'aria-live="polite"' in run_html


def test_the_stylesheet_honours_a_reduced_motion_preference():
    from pathlib import Path

    css = Path("static/css/main.css").read_text(encoding="utf-8")

    assert "prefers-reduced-motion" in css
    assert "color-scheme" in css
    assert "touch-action: manipulation" in css


def test_the_stylesheet_has_one_breakpoint_unit():
    """720px and 60rem were the same file disagreeing with its own warning."""
    import re
    from pathlib import Path

    css = Path("static/css/main.css").read_text(encoding="utf-8")
    units = {m.group(1) for m in re.finditer(r"@media \(max-width: \d+(px|rem)\)", css)}

    assert units == {"px"}, units


def test_no_dead_rules_are_left_behind():
    """`.admin-nav` was 21 lines styling a component no template rendered."""
    from pathlib import Path

    css = Path("static/css/main.css").read_text(encoding="utf-8")

    assert ".admin-nav" not in css
    assert ".right {" not in css


# -- the spec report on a component -------------------------------------------


def _report(passes: bool = True):
    """A real Report from the real engine, not a stand-in."""
    from app.core.rules import audit_agents

    body = "# X\n\nHome: https://x.example\n"
    if not passes:
        body += "\nCall https://invented.example/api for stock.\n"
    return audit_agents(body, site_url="https://x.example", verified_urls=["https://x.example"])


def test_a_generated_file_shows_its_spec_score():
    from app.core.components import SiteType
    from app.core.site_state import derive

    status = derive("https://x.example", SiteType.CONTENT, artifacts={"llms.txt": "# x"})
    html = _render(
        "family.html",
        statuses=[status.by_key("llms-txt")],
        markable=set(),
        reports={"llms-txt": _report()},
    )

    assert "Spec check" in html
    assert "/100" in html


def test_a_failing_file_names_the_rule_that_failed():
    """A score with no reason is a number nobody can act on."""
    from app.core.components import SiteType
    from app.core.site_state import derive

    status = derive("https://x.example", SiteType.CONTENT, artifacts={"llms.txt": "# x"})
    html = _render(
        "family.html",
        statuses=[status.by_key("llms-txt")],
        markable=set(),
        reports={"llms-txt": _report(passes=False)},
    )

    assert "AGT-004" in html
    assert "no probe confirmed" in html
    assert "invented.example" in html, "the example, so it can be found and removed"


def test_an_artifact_with_no_rule_set_says_so_rather_than_implying_a_pass():
    """Every artifact has rules today. A future one added without them must not
    render as a silent pass.

    Written against a `judged` mapping that omits the artifact rather than
    against a specific filename, so it keeps testing the behaviour after the
    gap it was written for was closed.
    """
    from app.core.components import SiteType
    from app.core.site_state import derive

    status = derive("https://x.example", SiteType.CONTENT, artifacts={"robots.txt": "# r"})
    html = _render(
        "family.html",
        statuses=[status.by_key("robots")],
        markable=set(),
        reports={},
        judged={"llms.txt": "index"},
    )

    assert "No spec rules exist" in html
    assert "Spec check" not in html


def test_every_generated_artifact_now_has_a_rule_set():
    """The gap the CRW, HDR and CAT sets were written to close."""
    from app.core.components import COMPONENTS
    from app.core.evidence import JUDGED_BY

    artifacts = {c.artifact for c in COMPONENTS if c.artifact}

    assert artifacts - set(JUDGED_BY) == set(), "an artifact is generated and unchecked"


def test_every_nav_item_names_an_icon():
    """A missing icon renders the fallback dot, which is legible but wrong.

    The failure it guards is quiet: an item added without one still works, still
    lines up, and just carries a circle for a glyph until somebody notices.
    """
    groups = build_nav("/clients", "example.com", is_admin=True)

    without = [item.title for group in groups for item in group.items if not item.icon]

    assert without == [], f"nav items with no icon: {without}"


def test_every_family_has_its_own_icon():
    """`FAMILY_ICONS` is a second map keyed like `FAMILY_LABELS`.

    Adding a seventh family to the labels and forgetting this one is the exact
    way the placeholder gets shipped, and `.get(family, "")` is what makes it
    silent rather than an error.
    """
    from app.core.components import FAMILY_LABELS
    from app.nav import FAMILY_ICONS

    assert set(FAMILY_ICONS) == set(FAMILY_LABELS)


def test_the_icon_macro_draws_every_name_the_nav_asks_for():
    """The nav and the macro are two files agreeing by string.

    Asserted against the rendered SVG rather than the source, because a name the
    macro does not know falls through to `{% else %}` and still renders -- so the
    only observable difference is which shape comes out.
    """
    from jinja2 import Environment, FileSystemLoader, StrictUndefined

    env = Environment(
        loader=FileSystemLoader("templates"), undefined=StrictUndefined, autoescape=True
    )
    macro = env.get_template("partials/icons.html").module.icon
    fallback = str(macro("no-such-icon"))

    names = {
        item.icon
        for group in build_nav("/clients", "example.com", is_admin=True)
        for item in group.items
    }
    unknown = sorted(name for name in names if str(macro(name)) == fallback)

    assert unknown == [], f"icons.html has no shape for: {unknown}"


def test_the_render_fixture_supplies_every_key_the_real_context_builds():
    """`_render` hand-rolls what the context builders return, so it drifts.

    It drifted the moment share links added six keys: nine templates failed under
    StrictUndefined at once, and the cause was the fixture rather than any of
    them. This reads the real function's dict literal so the next drift fails
    here, in one place, naming the missing key.
    """
    import ast
    import inspect

    from app import main

    # No dedent: `_component_context` is module-level, so `getsource` already
    # starts at column 0. `cleandoc` strips the body's own indentation and turns
    # valid source into an IndentationError.
    tree = ast.parse(
        inspect.getsource(main._component_context) + inspect.getsource(main._settings_context)
    )
    returned = {
        key.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict)
        for key in node.value.keys
        if isinstance(key, ast.Constant) and isinstance(key.value, str)
    }
    assert returned, "could not read the context dict; has the function changed shape?"

    supplied = set(_render_context())

    assert not (returned - supplied), f"the fixture is missing: {sorted(returned - supplied)}"
