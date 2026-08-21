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


# Two kinds of template, and only one of them makes a claim.
#
# A service template -- MCP card, A2A card, OAuth metadata, skills, api-catalog,
# WebMCP -- asserts that something exists and answers, so every value that would
# be a claim is a placeholder. A server-config snippet asserts nothing: it is
# real configuration to apply, and inventing placeholders in it would make it
# useless. Both must still say they are not files to publish.
SERVICE_TEMPLATES = {"mcp-card", "a2a-card", "oauth-resource", "skills", "api-catalog", "webmcp"}
CONFIG_TEMPLATES = {"link-header", "markdown-negotiation"}


def test_every_template_says_it_is_not_a_file_to_publish():
    """A template that looks finished is a template somebody publishes."""
    for key, body in build_templates("https://x.example").items():
        assert "not a file to publish" in body.lower(), key


def test_service_templates_carry_placeholders_where_a_claim_would_go():
    for key, body in build_templates("https://x.example").items():
        if key in SERVICE_TEMPLATES:
            assert PLACEHOLDER in body, key


def test_config_snippets_are_real_configuration_not_placeholders():
    """Filling these with REPLACE_ME would make them useless to paste."""
    for key, body in build_templates("https://x.example").items():
        if key in CONFIG_TEMPLATES:
            assert PLACEHOLDER not in body, key


def test_every_templated_component_falls_into_one_of_the_two_kinds():
    """A new template belonging to neither would be tested by nothing."""
    templated = {c.key for c in COMPONENTS if c.templated}

    assert templated == SERVICE_TEMPLATES | CONFIG_TEMPLATES


def test_a_platform_that_cannot_set_headers_is_told_so_rather_than_given_a_snippet():
    """Shopify and Wix cannot set custom response headers on the primary domain.

    A snippet they would spend an afternoon failing to apply is worse than a
    sentence saying it is not achievable there.
    """
    shopify = build_templates("https://x.example", "X", "shopify")["link-header"]
    wordpress = build_templates("https://x.example", "X", "wordpress")["link-header"]

    assert "not achievable" in shopify
    assert "add_header" not in shopify
    assert "add_header" in wordpress


# -- one source of truth, asserted by identity --------------------------------


def test_there_is_exactly_one_effort_enum():
    """Identity, not equality -- equality is what masked the bug.

    `bundle` used to define its own `Effort` with the same members. Because both
    were StrEnum they hashed identically, so `EFFORT_LABELS[components.Effort.X]`
    resolved across the two classes and the handover page worked by accident. A
    plain-Enum cross-lookup raises immediately, so the page was one refactor away
    from a KeyError.
    """
    from app.core import bundle

    assert bundle.Effort is Effort
    assert bundle.Effort.DROP_IN is Effort.DROP_IN


def test_bundle_defines_no_second_copy_of_the_registry_tables():
    """Re-exporting is fine. Redefining is how the two drift apart."""
    import inspect

    from app.core import bundle

    source = inspect.getsource(bundle)
    for redefinition in (
        "class Effort(",
        "EFFORT_LABELS: dict",
        "EFFORT_OWNERS: dict",
        "HEADER_HINTS: dict",
    ):
        assert redefinition not in source, redefinition


def test_every_effort_label_and_owner_is_keyed_by_a_registry_member():
    from app.core.components import EFFORT_LABELS, EFFORT_OWNERS

    for table in (EFFORT_LABELS, EFFORT_OWNERS):
        assert set(table) == set(Effort)
        for key in table:
            assert isinstance(key, Effort)


def test_the_developer_handover_is_projected_not_hand_written():
    """A hand-written list agrees with the checklist until someone adds a
    component, at which point the audit knows about it and the handover does not.
    """
    import inspect

    from app.core import bundle

    assert "for_developer(" in inspect.getsource(bundle._deployment_tasks)


# -- template becomes artefact once the endpoint answers ----------------------


def test_a_verified_endpoint_turns_a_template_into_a_real_file():
    """The transition the whole templating design exists for.

    Until the service answers, the component offers scaffolding that cannot be
    published. Once `verify_declared` confirms it, the same component produces a
    genuine artefact and the banner goes.
    """
    from app.core.bundle import DeclaredEndpoint

    templates = build_templates("https://x.example")

    unverified = derive("https://x.example", SiteType.APP_API, templates=templates)
    assert unverified.by_key("mcp-card").state is ComponentState.TEMPLATE
    assert not unverified.by_key("mcp-card").publishable

    # A verified endpoint is what a real artefact would be built from; the
    # template must not still be offered alongside it.
    verified = DeclaredEndpoint("mcp", "https://mcp.x.example/", verified=True, detail="200 json")
    assert verified.verified
    ready = derive(
        "https://x.example",
        SiteType.APP_API,
        artifacts={"ai-catalog.json": "{}"},
        templates={k: v for k, v in templates.items() if k != "mcp-card"},
    )
    assert ready.by_key("mcp-card").state is not ComponentState.TEMPLATE


def test_an_endpoint_that_does_not_answer_keeps_the_template_and_names_the_status():
    from app.core.bundle import DeclaredEndpoint, build_bundle
    from app.core.onboarding import brief_from_answers

    bundle = build_bundle(
        "https://x.example",
        brief_from_answers({"primary_action": "use_the_api", "mcp_server_url": "https://mcp.x/"}),
        declared=[DeclaredEndpoint("mcp", "https://mcp.x/", verified=False, detail="answered 404")],
        llms_txt="# l",
        agents_md="# a",
        ai_catalog="{}",
    )

    assert any("404" in note for note in bundle.notes)
    task = next(t for t in bundle.tasks if t.component == by_key("mcp-card").title)
    assert "404" in task.blocked_by
