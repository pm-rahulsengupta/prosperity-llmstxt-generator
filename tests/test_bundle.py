"""The per-scenario file set, and the work that produces no file.

Two things are being protected here. That a scenario gets the files it calls for
and is told which it does not need -- "we did not make you one" and "you do not
need one" are different answers. And that the half of the checklist requiring a
developer is carried rather than dropped, because a bundle of downloads silently
omits Link headers, Markdown negotiation and every Layer 1 item.
"""

from __future__ import annotations

import pytest

from app.core.bundle import (
    SCENARIO_COMPONENTS,
    DeclaredEndpoint,
    Effort,
    build_bundle,
    render_headers,
    render_robots,
)
from app.core.onboarding import BotPolicy, PrimaryAction, SiteBrief, brief_from_answers


def bundle_for(action: str, answers: dict | None = None, **kwargs):
    brief = brief_from_answers({"primary_action": action, **(answers or {})})
    return build_bundle(
        "https://x.example",
        brief,
        llms_txt="# llms",
        llms_full="# full",
        agents_md="# agents",
        ai_catalog="{}",
        **kwargs,
    )


# -- scenarios ----------------------------------------------------------------


def test_every_primary_action_has_a_scenario():
    """A goal with no manifest would silently produce the default set."""
    for action in PrimaryAction:
        if action is PrimaryAction.UNDECIDED:
            continue
        assert action.value in SCENARIO_COMPONENTS, action


def test_every_scenario_names_components_that_exist():
    """The reason the table is keyed on components rather than filenames.

    It caught its first mistake immediately: `llms-full.txt` was named here and
    had no component behind it, so the filename existed in this table and
    nowhere else in the tool.
    """
    from app.core.components import by_key

    for scenario, keys in SCENARIO_COMPONENTS.items():
        for key in keys:
            component = by_key(key)
            assert component is not None, f"{scenario} names unknown component {key}"
            assert component.artifact, f"{scenario} names {key}, which produces no file"


def test_a_shop_gets_the_catalog_and_a_firm_does_not():
    shop = bundle_for("shop_on_store")
    firm = bundle_for("contact_agency")

    assert shop.get("ai-catalog.json") is not None
    assert firm.get("ai-catalog.json") is None
    assert "ai-catalog.json" in firm.not_needed


def test_a_publisher_gets_the_full_text_file():
    assert bundle_for("read_and_cite").get("llms-full.txt") is not None
    assert bundle_for("contact_agency").get("llms-full.txt") is None


def test_files_outside_the_scenario_are_named_rather_than_missing():
    firm = bundle_for("contact_agency")
    produced = {a.name for a in firm.artifacts}

    assert firm.not_needed
    assert all(name not in produced for name in firm.not_needed)


def test_a_file_the_scenario_wants_but_we_could_not_build_is_flagged():
    """Distinguished from "not needed", which is the whole point of the list."""
    brief = brief_from_answers({"primary_action": "contact_agency"})
    bundle = build_bundle("https://x.example", brief, llms_txt="", agents_md="# a")

    assert any("llms.txt" in note for note in bundle.notes)


# -- robots.txt ---------------------------------------------------------------


def test_allow_all_permits_training_and_search():
    body = render_robots(SiteBrief(ai_bot_policy=BotPolicy.ALLOW_ALL))

    assert "ai-train=yes" in body
    assert "Disallow: /" not in body


def test_search_only_blocks_the_training_bots_and_says_what_that_costs():
    """The Cloudflare trap, in the file itself.

    Blocking Training there also blocks Googlebot from 15 September 2026, and
    nobody discovers that from a tick-box.
    """
    body = render_robots(SiteBrief(ai_bot_policy=BotPolicy.ALLOW_SEARCH_ONLY))

    assert "ai-train=no, search=yes" in body
    assert "GPTBot" in body
    assert "Disallow: /" in body
    assert "Googlebot" in body


def test_blocking_everything_is_expressed_in_both_places():
    body = render_robots(SiteBrief(ai_bot_policy=BotPolicy.BLOCK_ALL))

    assert "ai-train=no, search=no" in body
    assert body.count("Disallow: /") >= 2


def test_robots_is_offered_as_an_addition_not_a_replacement():
    """A client's robots.txt carries rules we cannot see the reasons for.

    Handing them a file that drops those is how a tool takes a site out of Google.
    """
    body = render_robots(SiteBrief(ai_bot_policy=BotPolicy.ALLOW_ALL))
    assert "add these to your existing" in body.lower()

    task = next(t for t in bundle_for("contact_agency").tasks if t.component == "robots.txt")
    assert "do not replace" in task.platform_hint.lower()


# -- Link headers -------------------------------------------------------------


def test_headers_only_advertise_files_that_will_exist():
    """A Link header pointing at a 404 costs an agent a request and its trust."""
    with_llms = render_headers("https://x.example", has_llms=True, has_catalog=False)
    without = render_headers("https://x.example", has_llms=False, has_catalog=False)

    assert "llms.txt" in with_llms
    assert "llms.txt" not in without
    assert "ai-catalog" not in with_llms


def test_a_verified_openapi_url_is_advertised():
    bundle = build_bundle(
        "https://x.example",
        brief_from_answers({"primary_action": "use_the_api"}),
        declared=[DeclaredEndpoint("openapi", "https://x.example/openapi.json", verified=True)],
        llms_txt="# l",
        agents_md="# a",
        ai_catalog="{}",
    )
    assert "service-desc" in bundle.get("_headers").body


def test_an_unverified_openapi_url_is_not_advertised():
    bundle = build_bundle(
        "https://x.example",
        brief_from_answers({"primary_action": "use_the_api"}),
        declared=[DeclaredEndpoint("openapi", "https://x.example/openapi.json", verified=False)],
        llms_txt="# l",
        agents_md="# a",
        ai_catalog="{}",
    )
    assert "service-desc" not in bundle.get("_headers").body


# -- deployment dependencies --------------------------------------------------


def title(key: str) -> str:
    """Resolve a component's name through the registry.

    The tests used to hardcode labels like "Link headers" while the registry
    called it "Link HTTP header with agent-aware rels" -- two names for one
    thing, which is the drift the consolidation removed. Looking it up means a
    renamed component updates its own tests.
    """
    from app.core.components import by_key

    return by_key(key).title


def test_the_developer_work_is_carried_not_dropped():
    """Link headers, Markdown negotiation and the page checks produce no
    downloadable file, and a bundle of files alone hands over only the easy half.
    """
    tasks = {t.component for t in bundle_for("contact_agency").developer_tasks}

    for key in ("link-header", "markdown-negotiation", "cls", "semantic-html"):
        assert title(key) in tasks, key


def test_uploads_are_separated_from_everything_that_blocks_on_a_developer():
    grouped = bundle_for("contact_agency").tasks_by_effort()

    assert Effort.DROP_IN in grouped
    assert all(t.effort is Effort.DROP_IN for t in grouped[Effort.DROP_IN])
    assert Effort.SERVER_CONFIG in grouped


@pytest.mark.parametrize(
    ("platform", "expected"),
    [("shopify", "does not expose response headers"), ("wix", "not achievable")],
)
def test_platform_constraints_are_stated_rather_than_assumed_away(platform, expected):
    """Shopify and Wix cannot set custom response headers at all.

    Telling a client to add a Link header there wastes a developer's day and ends
    with the item still not done.
    """
    bundle = bundle_for("contact_agency", platform=platform)
    task = next(t for t in bundle.tasks if t.component == title("link-header"))

    assert expected in task.platform_hint


def test_an_unknown_platform_gets_generic_wording_rather_than_a_wrong_guess():
    bundle = bundle_for("contact_agency", platform="")
    task = next(t for t in bundle.tasks if t.component == title("link-header"))

    assert "wherever your host allows" in task.platform_hint


def test_a_declared_but_unreachable_service_becomes_an_infrastructure_task():
    bundle = build_bundle(
        "https://x.example",
        brief_from_answers({"primary_action": "use_the_api", "mcp_server_url": "https://mcp.x/"}),
        declared=[DeclaredEndpoint("mcp", "https://mcp.x/", verified=False, detail="answered 502")],
        llms_txt="# l",
        agents_md="# a",
        ai_catalog="{}",
    )
    task = next(t for t in bundle.tasks if t.component == title("mcp-card"))

    assert task.effort is Effort.INFRASTRUCTURE
    assert "502" in task.blocked_by
    assert any("not published" in note for note in bundle.notes)


def test_choosing_to_block_training_adds_a_cdn_task():
    """robots.txt alone does not produce that outcome; the CDN overrides it."""
    bundle = bundle_for("contact_agency", answers={"ai_bot_policy": "allow_search_only"})
    task = next(t for t in bundle.tasks if t.component == title("web-bot-auth"))

    assert "Googlebot" in task.platform_hint
