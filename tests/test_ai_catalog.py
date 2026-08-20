"""`/.well-known/ai-catalog.json` — Agentic Resource Discovery.

The only one of the three agent files with a real specification behind it: Google
plus ten partners, 17 June 2026, Apache 2.0. It is also a v0.9 draft with near-zero
adoption, and it is parsed by machines that will *connect* to whatever it lists, so
a wrong entry is a failed connection rather than a misleading sentence.

Most of these therefore test what the catalog refuses to publish.
"""

from __future__ import annotations

import json
from datetime import date

from app.core.ai_catalog import build_catalog, render_catalog
from app.scrape.agents_probe import ProbeResult, Surface, SurfaceState, parse_ucp
from app.scrape.tech_probe import Detection, Platform, TechProfile

UCP_JSON = (
    '{"ucp":{"version":"2026-04-08","services":{"dev.ucp.shopping":'
    '[{"transport":"mcp","endpoint":"https://s.myshopify.com/api/ucp/mcp"}]}}}'
)
STAMP = date(2026, 8, 20)


def rich_probe() -> ProbeResult:
    return ProbeResult(
        site_url="https://shop.example",
        platform="shopify",
        ucp=Surface(url="https://shop.example/.well-known/ucp", state=SurfaceState.PRESENT),
        agents_md=Surface(
            url="https://shop.example/agents.md",
            state=SurfaceState.PRESENT,
            content_type="text/markdown",
        ),
        llms_txt=Surface(
            url="https://shop.example/llms.txt",
            state=SurfaceState.PRESENT,
            content_type="text/plain",
        ),
        ucp_profile=parse_ucp(UCP_JSON),
    )


def tech(*endpoints: Detection) -> TechProfile:
    return TechProfile(
        site_url="https://shop.example", platform=Platform.SHOPIFY, endpoints=list(endpoints)
    )


# -- what gets catalogued -----------------------------------------------------


def test_a_ucp_endpoint_is_catalogued_with_its_transport():
    catalog = build_catalog(rich_probe())
    entry = next(e for e in catalog.entries if "ucp" in e.tags)

    assert entry.url == "https://s.myshopify.com/api/ucp/mcp"
    assert "mcp" in entry.tags
    assert ".well-known/ucp" in entry.evidence


def test_published_agent_files_are_catalogued():
    names = {e.display_name for e in build_catalog(rich_probe()).entries}

    assert "Agent instructions" in names
    assert "Content index for language models" in names


def test_a_sitemap_is_deliberately_not_catalogued():
    """robots.txt already advertises it and every crawler reads that first.

    Listing one here spends an agent's request to tell it something it knows.
    """
    catalog = build_catalog(
        rich_probe(),
        tech(Detection("Sitemap", "200 application/xml", "https://shop.example/sitemap.xml")),
    )

    assert not any("sitemap" in e.url for e in catalog.entries)


def test_a_verified_api_is_catalogued():
    catalog = build_catalog(
        rich_probe(),
        tech(
            Detection("WordPress REST API", "200 application/json", "https://shop.example/wp-json/")
        ),
    )
    entry = next(e for e in catalog.entries if "wp-json" in e.url)

    assert entry.media_type == "application/json"
    assert "answered" in entry.evidence


# -- what it refuses ----------------------------------------------------------


def test_a_site_with_nothing_verified_produces_no_catalog_worth_publishing():
    """A brochure site has no MCP server, no agent card and no API.

    Being early is defensible; being early and empty is not.
    """
    catalog = build_catalog(ProbeResult(site_url="https://firm.example"))

    assert not catalog.worth_publishing
    assert "noise" in catalog.notes[0]


def test_one_lonely_entry_is_not_worth_publishing():
    probe = ProbeResult(
        site_url="https://firm.example",
        llms_txt=Surface(
            url="https://firm.example/llms.txt",
            state=SurfaceState.PRESENT,
            content_type="text/plain",
        ),
    )
    catalog = build_catalog(probe)

    assert len(catalog.entries) == 1
    assert not catalog.worth_publishing


def test_a_soft_404_contributes_nothing():
    """The same rule as everywhere else: a 200 of HTML is a page, not a file."""
    probe = ProbeResult(
        site_url="https://x.example",
        agents_md=Surface(
            url="https://x.example/agents.md",
            state=SurfaceState.SOFT_404,
            status=200,
            content_type="text/html",
        ),
    )
    assert build_catalog(probe).entries == []


def test_no_ucp_profile_means_no_commerce_entry():
    probe = ProbeResult(site_url="https://shop.example", platform="shopify")
    assert not any("ucp" in e.tags for e in build_catalog(probe).entries)


# -- the rendered document ----------------------------------------------------


def test_the_rendered_catalog_is_valid_json_with_the_spec_shape():
    document = json.loads(render_catalog(build_catalog(rich_probe()), generated_on=STAMP))

    assert document["specVersion"] == "1.0"
    assert document["host"]["identifier"] == "https://shop.example"
    assert isinstance(document["entries"], list)
    for entry in document["entries"]:
        for required in ("identifier", "displayName", "type", "url", "description"):
            assert required in entry, required


def test_identifiers_follow_the_urn_convention_both_live_catalogs_use():
    for entry in build_catalog(rich_probe()).entries:
        assert entry.identifier.startswith("urn:air:shop.example:")


def test_the_document_is_dated_in_a_namespaced_field():
    """Not part of the spec, so namespaced. A file nobody can date is a file
    nobody can tell is stale, and this convention is eight weeks old."""
    document = json.loads(render_catalog(build_catalog(rich_probe()), generated_on=STAMP))

    assert document["x-generated"]["on"] == "2026-08-20"
    assert "specVersion" in document


def test_rendering_is_deterministic():
    catalog = build_catalog(rich_probe())
    assert render_catalog(catalog, STAMP) == render_catalog(catalog, STAMP)
    assert render_catalog(catalog, STAMP).endswith("\n")


def test_every_catalog_records_that_the_spec_is_a_draft():
    """Publishing this is being early, not being compliant."""
    assert any("draft" in note for note in build_catalog(rich_probe()).notes)
