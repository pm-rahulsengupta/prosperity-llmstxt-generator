"""Rendering agents.md.

Byte-pinned per profile under `UPDATE_GOLDEN`, like `test_golden_output_is_stable`
and the prompt snapshots. This file is an instruction manual an agent follows, so a
wording change that softens a constraint — "confirm before purchasing" becoming
"consider confirming" — is a behavioural change to every agent that reads it, and
has to arrive in review as a diff rather than as silence.
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import pytest

from app.core.agents_doc import Capability, PolicyLink, build_agents_doc
from app.core.agents_render import render_agents_liquid, render_agents_md
from app.core.ranking import (
    PATTERN_AGENCY,
    PATTERN_ECOMMERCE_RETAIL,
    PATTERN_PUBLISHER,
    PATTERN_SAAS,
)
from app.scrape.agents_probe import ProbeResult, Surface, SurfaceState, parse_ucp

GOLDEN = Path(__file__).parent / "fixtures" / "agents"
STAMP = date(2026, 8, 20)

UCP_JSON = (
    '{"ucp":{"version":"2026-04-08","supported_versions":'
    '{"2026-04-08":"a","2026-01-23":"b"},"services":{"dev.ucp.shopping":'
    '[{"transport":"mcp","endpoint":"https://s.myshopify.com/api/ucp/mcp",'
    '"version":"2026-04-08"}]}}}'
)


def shop_probe() -> ProbeResult:
    return ProbeResult(
        site_url="https://shop.example",
        platform="shopify",
        ucp=Surface(url="https://shop.example/.well-known/ucp", state=SurfaceState.PRESENT),
        llms_txt=Surface(url="https://shop.example/llms.txt", state=SurfaceState.PRESENT),
        ucp_profile=parse_ucp(UCP_JSON),
    )


def check(name: str, text: str) -> None:
    GOLDEN.mkdir(parents=True, exist_ok=True)
    path = GOLDEN / f"{name}.md"
    if os.environ.get("UPDATE_GOLDEN") == "1":
        path.write_text(text, encoding="utf-8", newline="")
    assert path.exists(), f"run once with UPDATE_GOLDEN=1 to create {path.name}"
    assert text == path.read_text(encoding="utf-8"), (
        f"{name} changed. If deliberate, regenerate with UPDATE_GOLDEN=1 so the "
        "wording change is reviewable — agents act on this text."
    )


def full_shop():
    doc = build_agents_doc(
        shop_probe(),
        PATTERN_ECOMMERCE_RETAIL,
        site_name="Shop Example",
        read_only=[
            Capability("Products", "https://shop.example/products.json", "sitemap"),
            Capability("Collections", "https://shop.example/collections.json", "sitemap"),
        ],
        policies=[
            PolicyLink("Refunds", "https://shop.example/policies/refund"),
            PolicyLink("Privacy", "https://shop.example/policies/privacy"),
        ],
        rate_limit_note="Up to 2 requests per second per agent.",
    )
    doc.summary = "> Shop Example sells merino footwear direct to consumers in Australia."
    doc.agent_guidance = "Canonical site: https://shop.example"
    return doc


# -- golden files ------------------------------------------------------------


def test_golden_shop():
    check("shop", render_agents_md(full_shop(), generated_on=STAMP))


def test_golden_agency_with_nothing_published():
    """The most common shape on the client list."""
    doc = build_agents_doc(
        ProbeResult(site_url="https://agency.example"),
        PATTERN_AGENCY,
        site_name="Agency Example",
    )
    doc.summary = "> Digital PR and SEO for Australian brands."
    check("agency_bare", render_agents_md(doc, generated_on=STAMP))


def test_golden_agency_with_contact_and_policies():
    doc = build_agents_doc(
        ProbeResult(site_url="https://agency.example"),
        PATTERN_AGENCY,
        site_name="Agency Example",
        read_only=[Capability("Services", "https://agency.example/services/", "crawl")],
        policies=[PolicyLink("Privacy", "https://agency.example/privacy/")],
        contact_url="https://agency.example/contact/",
    )
    doc.summary = "> Digital PR and SEO for Australian brands."
    check("agency_full", render_agents_md(doc, generated_on=STAMP))


def test_golden_publisher():
    doc = build_agents_doc(
        ProbeResult(site_url="https://news.example"),
        PATTERN_PUBLISHER,
        site_name="News Example",
        read_only=[Capability("Search", "https://news.example/search", "crawl")],
        rate_limit_note="One request per second; identify yourself in User-Agent.",
    )
    doc.summary = "> Independent motoring journalism."
    check("publisher", render_agents_md(doc, generated_on=STAMP))


def test_golden_shopify_liquid():
    check("shopify_liquid", render_agents_liquid(full_shop(), generated_on=STAMP))


# -- determinism -------------------------------------------------------------


def test_rendering_is_byte_stable():
    doc = full_shop()
    assert render_agents_md(doc, STAMP) == render_agents_md(doc, STAMP)


def test_the_file_ends_with_a_single_newline():
    assert render_agents_md(full_shop(), STAMP).endswith("\n")
    assert not render_agents_md(full_shop(), STAMP).endswith("\n\n")


def test_there_is_exactly_one_h1():
    body = render_agents_md(full_shop(), STAMP)
    assert len([ln for ln in body.splitlines() if ln.startswith("# ")]) == 1


# -- the invariant, at the rendered-text level -------------------------------


@pytest.mark.parametrize(
    "profile", [PATTERN_AGENCY, PATTERN_PUBLISHER, PATTERN_SAAS, PATTERN_ECOMMERCE_RETAIL]
)
def test_no_endpoint_is_ever_rendered_without_a_ucp_profile(profile):
    """The invented-endpoint test, asserted on the output rather than the model.

    `claimed_urls` is checked elsewhere; this checks the bytes, because that is
    what an agent reads.
    """
    doc = build_agents_doc(ProbeResult(site_url="https://x.example"), profile)
    body = render_agents_md(doc, STAMP)

    for forbidden in ("/api/ucp/mcp", "myshopify", "/cart", "/checkout.json"):
        assert forbidden not in body, f"{profile}: leaked {forbidden}"


def test_a_non_shop_never_renders_checkout_language():
    doc = build_agents_doc(shop_probe(), PATTERN_AGENCY, site_name="Firm")
    body = render_agents_md(doc, STAMP).lower()

    assert "checkout" not in body.split("## not supported")[0]
    assert "cart" not in body.split("## not supported")[0]


def test_a_site_with_nothing_published_still_tells_an_agent_what_not_to_do():
    """Why a document with no evidence is still worth generating.

    An agent that knows there is no transaction endpoint stops looking, rather
    than guessing at /cart and /checkout.
    """
    doc = build_agents_doc(ProbeResult(site_url="https://firm.example"), PATTERN_AGENCY)
    body = render_agents_md(doc, STAMP)

    assert "## Not supported" in body
    assert "does not sell through an agent protocol" in body


def test_absence_is_only_announced_where_it_was_expected():
    """A shop's profile has no contact section, so it must not report one missing.

    A file that reports things it was never going to have teaches an agent to
    distrust the rest of the list.
    """
    shop = render_agents_md(full_shop(), STAMP)
    assert "No contact endpoint" not in shop

    bare = build_agents_doc(ProbeResult(site_url="https://firm.example"), PATTERN_AGENCY)
    assert "No contact endpoint" in render_agents_md(bare, STAMP)


# -- the footer claims a convention, not a standard --------------------------


def test_the_footer_does_not_claim_conformance_to_a_specification():
    """agents.md has no ratified spec, and Shopify changed six endpoints without
    announcement in May 2026. Claiming conformance would be overstating."""
    body = render_agents_md(full_shop(), STAMP)

    assert "convention rather than a ratified specification" in body
    assert "2026-08-20" in body


# -- the Shopify override ----------------------------------------------------


def test_the_liquid_template_references_no_unavailable_theme_object():
    """Only `request` and `agents` exist in this template context.

    A template referencing `shop` or `collections` renders empty, which would ship
    a broken file to the merchant's live storefront.
    """
    import re

    liquid = render_agents_liquid(full_shop(), generated_on=STAMP)
    # Only what Liquid actually evaluates. A first version searched the raw text
    # and matched the fixture's own domain, `shop.example`, as a reference to the
    # `shop` object -- a test failing on prose rather than on the thing it guards.
    expressions = re.findall(r"\{\{(.*?)\}\}", liquid, re.S)
    expressions += re.findall(r"\{%(.*?)%\}", liquid, re.S)
    evaluated = " ".join(expressions)

    for unavailable in ("shop.", "collections.", "product.", "cart.", "customer."):
        assert unavailable not in evaluated, unavailable
    # And prove the guard can fire, rather than passing because it found nothing.
    assert "{%-" in liquid


def test_the_liquid_template_says_where_to_put_it():
    liquid = render_agents_liquid(full_shop(), generated_on=STAMP)

    assert "templates/agents.md.liquid" in liquid
    assert "replaces Shopify's default" in liquid


def test_the_liquid_template_contains_the_rendered_file():
    liquid = render_agents_liquid(full_shop(), generated_on=STAMP)
    assert render_agents_md(full_shop(), generated_on=STAMP) in liquid
