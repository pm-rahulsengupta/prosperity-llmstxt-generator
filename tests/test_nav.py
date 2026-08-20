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


def test_the_generate_group_always_offers_both_files():
    items = flat(build_nav("/"))
    assert "llms.txt" in items
    assert "agents.md" in items


def test_the_index_is_active_only_on_the_index():
    """`/` is a prefix of every path and would otherwise light everywhere."""
    assert flat(build_nav("/"))["llms.txt"].active
    assert not flat(build_nav("/agents"))["llms.txt"].active
    assert not flat(build_nav("/admin"))["llms.txt"].active


def test_a_run_page_lights_the_section_it_belongs_to():
    """A run is the output of the llms.txt flow.

    Leaving the sidebar entirely dark on the page an operator spends most of
    their time on is a worse answer than naming its section.
    """
    assert flat(build_nav("/runs/abc-123"))["llms.txt"].active


def test_the_agents_tab_lights_on_its_own_pages():
    items = flat(build_nav("/agents"))
    assert items["agents.md"].active
    assert not items["llms.txt"].active


def test_site_items_are_shown_but_marked_when_there_is_no_domain():
    """GEO Tracker keeps its Team page visible and lets the page explain itself.

    Hiding an item leaves an operator hunting for a page that exists.
    """
    brief = flat(build_nav("/"))["Brief"]

    assert brief.disabled
    assert brief.hint
    assert brief.url == "/", "with no domain it points at the picker, not a dead path"


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
    assert "agents.md" in html


# -- the eight new pages ------------------------------------------------------


def _render(template: str, **extra):
    """Render through the app's own environment with StrictUndefined.

    `base.html` reads globals registered on that environment, so a fresh one
    would test a template that does not exist. Strict undefined turns a context
    key a route forgets into a failure here instead of a 500 in production.
    """
    from types import SimpleNamespace

    from jinja2 import StrictUndefined

    from app.core.components import SiteType
    from app.core.site_state import derive
    from app.core.templates_lib import build_templates
    from app.main import templates as app_templates

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
        **extra,
    }

    env = app_templates.env
    previous, env.undefined = env.undefined, StrictUndefined
    try:
        return env.get_template(template).render(**context)
    finally:
        env.undefined = previous


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
