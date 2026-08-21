"""The component registry, and the four consumers that must not drift from it.

The same twenty-one things used to be modelled three times with nothing linking
them, so `robots.txt` appeared under three names and a component added to one was
invisible to the other two. These tests are what keeps that from coming back:
adding a component without wiring it fails the suite rather than appearing blank
on a page nobody checks.
"""

from __future__ import annotations

import pytest

from app.core.components import (
    COMPONENTS,
    ComponentState,
    Effort,
    Family,
    Priority,
    SiteType,
    applicable,
    by_family,
    by_key,
    for_client,
    for_developer,
)
from app.core.site_state import derive, manually_markable
from app.core.templates_lib import PLACEHOLDER, build_templates

# -- the registry is complete -------------------------------------------------


def test_every_component_is_fully_specified():
    """A component missing a field renders as a blank row nobody can action."""
    for component in COMPONENTS:
        assert component.key, component
        assert component.title, component.key
        assert component.verify, component.key
        assert component.why, component.key
        assert component.layer in (1, 2), component.key
        assert isinstance(component.priority, Priority), component.key
        assert isinstance(component.effort, Effort), component.key


def test_every_component_states_applicability_for_every_site_type():
    for component in COMPONENTS:
        for site_type in SiteType:
            assert site_type in component.applies, (component.key, site_type)


def test_keys_are_unique():
    keys = [c.key for c in COMPONENTS]
    assert len(keys) == len(set(keys))


def test_every_component_belongs_to_exactly_one_family():
    counted = sum(len(by_family(family)) for family in Family)
    assert counted == len(COMPONENTS)


def test_a_component_either_produces_something_or_says_why_not():
    """Anything with no artefact and no template must be a server behaviour.

    Otherwise it is a row that appears on a checklist and offers nothing, which
    is the shape of a component somebody forgot to wire up.
    """
    # Things a site *does* rather than files it serves, plus one the platform
    # writes for you: Shopify generates its own UCP document, so a tool
    # generating a competing one would be offering to overwrite the authority.
    behaviours = {
        "sitemap",
        "link-header",
        "markdown-negotiation",
        "content-signals",
        "web-bot-auth",
        "commerce-protocols",
    }
    for component in COMPONENTS:
        if component.generated or component.templated or component.layer == 1:
            continue
        assert component.key in behaviours, (
            f"{component.key} produces nothing and is not a behaviour"
        )


# -- the checklist is a projection, not a copy --------------------------------


def test_the_readiness_checklist_is_the_registry():
    """One list. The audit and the tabs cannot disagree about what exists."""
    from app.scrape.readiness import CHECKLIST

    assert CHECKLIST is COMPONENTS


# -- client and developer lists partition the work ----------------------------


@pytest.mark.parametrize("site_type", list(SiteType))
def test_client_and_developer_lists_cover_everything_exactly_once(site_type):
    """A component on neither list is one nobody will ever do."""
    client = {c.key for c in for_client(site_type)}
    dev = {c.key for c in for_developer(site_type)}
    every = {c.key for c in applicable(site_type)}

    assert client | dev == every
    assert not (client & dev)


def test_only_drop_ins_reach_the_client_list():
    """Mixing "upload this file" with "implement content negotiation" produces a
    list that stalls at the first item the reader cannot action."""
    for component in for_client(SiteType.CONTENT):
        assert component.effort is Effort.DROP_IN, component.key


def test_a_law_firm_is_not_asked_for_an_agent_card():
    """Applicability is what makes the number mean anything."""
    keys = {c.key for c in applicable(SiteType.CONTENT)}

    assert "a2a-card" not in keys
    assert "commerce-protocols" not in keys
    assert "llms-txt" in keys


# -- state derivation ---------------------------------------------------------


def test_a_published_component_is_live_and_offers_no_replacement():
    """Offering to replace a working file invites someone to overwrite it."""
    from app.scrape.readiness import CheckResult, CheckState, ReadinessReport

    report = ReadinessReport(
        site_url="https://x.example",
        site_type=SiteType.CONTENT,
        results=[CheckResult(by_key("robots"), CheckState.PASS, "200 text/plain")],
    )
    status = derive(
        "https://x.example", SiteType.CONTENT, readiness=report, artifacts={"robots.txt": "..."}
    )

    robots = status.by_key("robots")
    assert robots.state is ComponentState.LIVE
    assert not robots.publishable


def test_a_generated_artefact_is_ready_and_publishable():
    status = derive("https://x.example", SiteType.CONTENT, artifacts={"llms.txt": "# x"})
    llms = status.by_key("llms-txt")

    assert llms.state is ComponentState.READY
    assert llms.publishable
    assert llms.artifact_name == "llms.txt"


def test_a_template_is_never_publishable():
    """The load-bearing property. A placeholder must not reach a web root by any
    route, so no download path will serve one as final."""
    templates = build_templates("https://x.example")
    status = derive("https://x.example", SiteType.APP_API, templates=templates)

    card = status.by_key("mcp-card")
    assert card.state is ComponentState.TEMPLATE
    assert not card.publishable
    assert not card.artifact_name
    assert PLACEHOLDER in card.template_body


def test_a_component_is_never_both_artefact_and_template():
    templates = build_templates("https://x.example")
    status = derive(
        "https://x.example",
        SiteType.APP_API,
        artifacts={"llms.txt": "# x"},
        templates=templates,
    )
    for item in status.statuses:
        assert not (item.artifact_name and item.template_body), item.key


def test_not_applicable_wins_over_everything():
    """Scoring a law firm on its agent card makes the number meaningless."""
    templates = build_templates("https://x.example")
    status = derive("https://x.example", SiteType.CONTENT, templates=templates)

    assert status.by_key("a2a-card").state is ComponentState.NOT_APPLICABLE


# -- manual marks -------------------------------------------------------------


def test_only_the_undetectable_components_may_be_marked_by_hand():
    """Ticking `llms.txt` while it 404s puts a false claim in a client report."""
    markable = {c.key for c in COMPONENTS if manually_markable(c)}

    assert markable == {"cls", "cursor", "tap-targets", "overlays", "webmcp", "web-bot-auth"}
    for key in ("llms-txt", "robots", "agents-md", "semantic-html", "roles", "labels"):
        assert key not in markable


def test_a_newly_probeable_check_stops_being_hand_markable_automatically():
    """The markable set is derived from what the prober actually handles.

    It began as a hardcoded copy and drifted the moment two WCAG checks were
    added: they became probe-decided and stayed hand-markable, so someone could
    have ticked "no deprecated ARIA roles" on a page carrying one.
    """
    from app.scrape.readiness import STATIC_LAYER1

    for key in STATIC_LAYER1:
        assert not manually_markable(by_key(key)), key


def test_a_mark_makes_an_undetectable_component_live():
    status = derive("https://x.example", SiteType.CONTENT, marks={"cls": "rahul@example.com"})
    cls = status.by_key("cls")

    assert cls.state is ComponentState.LIVE
    assert "rahul@example.com" in cls.detail


def test_a_mark_on_a_detectable_component_is_ignored():
    """Defence in depth: the route refuses it and the derivation ignores it."""
    status = derive("https://x.example", SiteType.CONTENT, marks={"llms-txt": "someone"})

    assert status.by_key("llms-txt").state is not ComponentState.LIVE


def test_the_probe_beats_a_mark_where_both_exist():
    """Evidence outranks assertion, in the one place they can disagree."""
    from app.scrape.readiness import CheckResult, CheckState, ReadinessReport

    report = ReadinessReport(
        site_url="https://x.example",
        site_type=SiteType.CONTENT,
        results=[CheckResult(by_key("robots"), CheckState.FAIL, "404")],
    )
    status = derive(
        "https://x.example", SiteType.CONTENT, readiness=report, marks={"robots": "someone"}
    )

    assert status.by_key("robots").state is not ComponentState.LIVE


# -- templates ----------------------------------------------------------------


def test_every_templated_component_has_a_template():
    templates = build_templates("https://x.example")
    for component in COMPONENTS:
        if component.templated:
            assert component.key in templates, component.key


def test_every_template_warns_and_carries_placeholders():
    """A template that looks finished is a template somebody publishes."""
    for key, body in build_templates("https://x.example").items():
        assert PLACEHOLDER in body, key
        assert "not a file to publish" in body, key
