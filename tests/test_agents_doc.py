"""The agents.md content model.

Most of these assert what the document *refuses* to say. An agents.md is acted on
rather than read, so an unverified claim is an instruction that fails against the
client's live site — a different class of defect from a wrong description in
llms.txt, and the reason the invariants here are absolute rather than tuned.
"""

from __future__ import annotations

import pytest

from app.core.agents_doc import (
    PROFILE_SECTIONS,
    TRANSACTIONAL_SECTIONS,
    Capability,
    PolicyLink,
    Section,
    build_agents_doc,
    transacts,
)
from app.core.ranking import (
    PATTERN_AGENCY,
    PATTERN_ECOMMERCE,
    PATTERN_ECOMMERCE_RETAIL,
    PATTERN_LOCAL,
    PATTERN_PROFESSIONAL,
    PATTERN_PUBLISHER,
    PATTERN_SAAS,
)
from app.scrape.agents_probe import ProbeResult, Surface, SurfaceState, parse_ucp

UCP_JSON = (
    '{"ucp":{"version":"2026-04-08","services":{"dev.ucp.shopping":'
    '[{"transport":"mcp","endpoint":"https://s.myshopify.com/api/ucp/mcp",'
    '"version":"2026-04-08"}]}}}'
)

READ_ONLY = [Capability("Products", "https://shop.com/products.json", "found in sitemap")]
POLICIES = [PolicyLink("Privacy", "https://shop.com/policies/privacy")]


def shopify_probe(site: str = "https://shop.com") -> ProbeResult:
    return ProbeResult(
        site_url=site,
        platform="shopify",
        ucp=Surface(url=f"{site}/.well-known/ucp", state=SurfaceState.PRESENT),
        ucp_profile=parse_ucp(UCP_JSON),
    )


def bare_probe(site: str = "https://firm.com") -> ProbeResult:
    """A site with nothing published — the common case on the client list."""
    return ProbeResult(site_url=site)


# -- the invariant: never claim what was not verified -------------------------


def test_a_shop_with_no_ucp_profile_never_mentions_an_endpoint():
    """The defect the module exists to prevent, stated directly.

    A generated file telling an agent to POST to an endpoint that does not exist
    makes the client's site look broken to every assistant that reads it.
    """
    doc = build_agents_doc(bare_probe(), PATTERN_ECOMMERCE_RETAIL, read_only=READ_ONLY)

    assert doc.claimed_urls == ["https://shop.com/products.json"] or not any(
        "ucp" in url for url in doc.claimed_urls
    )
    assert not doc.has(Section.COMMERCE_PROTOCOL)
    assert not doc.has(Section.AGENT_FLOW)


@pytest.mark.parametrize(
    "profile",
    [PATTERN_AGENCY, PATTERN_PROFESSIONAL, PATTERN_LOCAL, PATTERN_PUBLISHER, PATTERN_SAAS],
)
def test_a_non_shop_never_carries_a_commerce_endpoint_even_with_a_ucp_profile(profile):
    """A services site with a stray UCP document is a misconfiguration, not a shop.

    This was a live defect: the agency profile dropped every commerce section and
    still listed `s.myshopify.com/api/ucp/mcp` among its claims — a claim
    surviving the removal of the section that justified it.
    """
    doc = build_agents_doc(shopify_probe(), profile, read_only=READ_ONLY)

    assert not any("ucp" in url for url in doc.claimed_urls), doc.claimed_urls
    for section in TRANSACTIONAL_SECTIONS:
        assert not doc.has(section), section


def test_a_shop_with_a_verified_profile_does_carry_the_endpoint():
    """The rule is "verified only", not "never" — the positive case must work."""
    doc = build_agents_doc(shopify_probe(), PATTERN_ECOMMERCE_RETAIL, read_only=READ_ONLY)

    assert "https://s.myshopify.com/api/ucp/mcp" in doc.claimed_urls
    assert doc.has(Section.COMMERCE_PROTOCOL)
    assert doc.ucp_version == "2026-04-08"


def test_every_capability_carries_its_evidence():
    """What makes a line auditable when a client asks where it came from."""
    doc = build_agents_doc(shopify_probe(), PATTERN_ECOMMERCE_RETAIL)

    assert doc.capabilities
    for capability in doc.capabilities:
        assert capability.evidence
        assert ".well-known/ucp" in capability.evidence


def test_a_claimed_url_is_never_produced_from_a_dropped_section():
    """`claimed_urls` derives from surviving sections, not populated fields.

    Otherwise the validator checks something the file never says, and a leak looks
    legitimate because it appears in the list the validator walks.
    """
    doc = build_agents_doc(bare_probe(), PATTERN_AGENCY, policies=POLICIES, contact_url="")

    assert doc.has(Section.POLICIES)
    assert "https://shop.com/policies/privacy" in doc.claimed_urls
    assert not doc.has(Section.CONTACT)


# -- omission is recorded, not silent -----------------------------------------


def test_every_dropped_section_says_why_it_was_dropped():
    """A file missing its commerce section looks identical whether the site has no
    UCP endpoint or the probe timed out, and those need different responses."""
    doc = build_agents_doc(bare_probe(), PATTERN_ECOMMERCE_RETAIL)

    assert doc.omitted
    for omission in doc.omitted:
        assert omission.reason, omission.section

    reasons = {o.section: o.reason for o in doc.omitted}
    assert "well-known/ucp" in reasons[Section.COMMERCE_PROTOCOL]


def test_a_site_with_nothing_verified_still_produces_a_usable_file():
    """The most common starting point on the client list.

    `prosperitymedia.com.au`, `carsguide.com.au` and `stripe.com` all publish
    nothing today. "This site does not transact" is a complete and useful answer,
    so identity and not-supported survive with no evidence at all.
    """
    doc = build_agents_doc(bare_probe(), PATTERN_AGENCY)

    assert doc.sections == (Section.IDENTITY, Section.NOT_SUPPORTED)
    assert doc.claimed_urls == []


# -- profile shapes -----------------------------------------------------------


def test_only_shops_are_transactional():
    for profile in (PATTERN_ECOMMERCE_RETAIL, PATTERN_ECOMMERCE):
        assert transacts(profile)
    for profile in (PATTERN_AGENCY, PATTERN_PROFESSIONAL, PATTERN_LOCAL, PATTERN_PUBLISHER):
        assert not transacts(profile)


def test_no_non_transactional_profile_lists_a_transactional_section():
    """Enforced at the table, so a new profile cannot quietly acquire checkout."""
    for profile, sections in PROFILE_SECTIONS.items():
        if transacts(profile):
            continue
        assert not (set(sections) & TRANSACTIONAL_SECTIONS), profile


def test_every_profile_can_say_what_it_does_not_support():
    """The section that is most useful exactly when everything else is missing."""
    for profile, sections in PROFILE_SECTIONS.items():
        assert Section.NOT_SUPPORTED in sections, profile
        assert Section.IDENTITY in sections, profile


def test_a_publisher_gets_attribution_and_a_shop_does_not():
    """Attribution matters where content is lifted wholesale for nothing back."""
    publisher = build_agents_doc(bare_probe(), PATTERN_PUBLISHER, read_only=READ_ONLY)
    shop = build_agents_doc(shopify_probe(), PATTERN_ECOMMERCE_RETAIL, read_only=READ_ONLY)

    assert publisher.has(Section.ATTRIBUTION)
    assert not shop.has(Section.ATTRIBUTION)


def test_an_unknown_profile_falls_back_to_the_safest_shape():
    """Agency shape: read, contact, and no transaction verbs anywhere."""
    doc = build_agents_doc(shopify_probe(), "not_a_real_profile", read_only=READ_ONLY)

    assert not any(doc.has(s) for s in TRANSACTIONAL_SECTIONS)


# -- sections need evidence, not just permission ------------------------------


def test_a_section_needs_both_permission_and_evidence():
    """The conjunction is the design.

    The profile table alone would let a shop publish an empty commerce section;
    the probe alone would let a law firm publish a checkout flow.
    """
    with_evidence = build_agents_doc(bare_probe(), PATTERN_AGENCY, read_only=READ_ONLY)
    without = build_agents_doc(bare_probe(), PATTERN_AGENCY)

    assert Section.READ_ONLY in PROFILE_SECTIONS[PATTERN_AGENCY]
    assert with_evidence.has(Section.READ_ONLY)
    assert not without.has(Section.READ_ONLY)


def test_rate_limits_appear_only_when_the_brief_supplied_them():
    """We do not invent a number a client never agreed to advertise."""
    silent = build_agents_doc(bare_probe(), PATTERN_PUBLISHER, read_only=READ_ONLY)
    stated = build_agents_doc(
        bare_probe(), PATTERN_PUBLISHER, read_only=READ_ONLY, rate_limit_note="1 request/second"
    )

    assert not silent.has(Section.RATE_LIMITS)
    assert stated.has(Section.RATE_LIMITS)


def test_probe_notes_are_carried_onto_the_document():
    """Soft-404s and unreachable surfaces have to reach the operator."""
    probe = bare_probe()
    probe.notes.append("https://firm.com/agents.md — answers 200 with HTML")
    doc = build_agents_doc(probe, PATTERN_AGENCY)

    assert any("HTML" in note for note in doc.notes)
