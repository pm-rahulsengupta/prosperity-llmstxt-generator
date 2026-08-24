"""The probe survives a round trip through JSONB, or the pages lie.

A codec that loses a field does not raise -- it returns a plausible object with
the wrong values, and the page renders a confident number that no probe produced.
So these tests compare whole objects rather than spot-checking fields.
"""

from __future__ import annotations

import json

from app.core.components import SiteType
from app.core.snapshot import (
    probe_from_dict,
    probe_to_dict,
    readiness_from_dict,
    readiness_to_dict,
    tech_from_dict,
    tech_to_dict,
)
from app.scrape.agents_probe import ProbeResult, Surface, SurfaceState, UcpProfile, UcpService
from app.scrape.readiness import CheckResult, CheckState, ReadinessReport
from app.scrape.tech_probe import Detection, Platform, TechProfile


def a_probe() -> ProbeResult:
    return ProbeResult(
        site_url="https://x.example",
        llms_txt=Surface(
            url="https://x.example/llms.txt",
            state=SurfaceState.PRESENT,
            status=200,
            content_type="text/markdown",
            body="# a very long document" * 500,
        ),
        agents_md=Surface(url="https://x.example/agents.md", state=SurfaceState.ABSENT, status=404),
        ucp=Surface(
            url="https://x.example/.well-known/ucp", state=SurfaceState.PRESENT, status=200
        ),
        ucp_profile=UcpProfile(
            version="2026-01",
            supported_versions=("2026-01", "2025-11"),
            services=(
                UcpService(
                    name="catalog", version="1", transport="mcp", endpoint="https://mcp.x.example"
                ),
            ),
        ),
        platform="shopify",
        notes=["Detected Shopify from a storefront header."],
    )


def test_a_probe_round_trips():
    restored = probe_from_dict(probe_to_dict(a_probe()))
    original = a_probe()

    assert restored.site_url == original.site_url
    assert restored.platform == original.platform
    assert restored.notes == original.notes
    assert restored.has_ucp == original.has_ucp
    assert restored.verified_endpoints == original.verified_endpoints
    assert [s.url for s in restored.surfaces()] == [s.url for s in original.surfaces()]
    assert [s.state for s in restored.surfaces()] == [s.state for s in original.surfaces()]


def test_the_stored_row_is_json_serialisable():
    """JSONB rejects anything json cannot encode -- enums included."""
    json.dumps(probe_to_dict(a_probe()))
    json.dumps(readiness_to_dict(a_report()))
    json.dumps(tech_to_dict(a_tech()))


def test_a_surfaces_body_is_not_stored():
    """It is the client's whole llms.txt, never read back, and it would make the
    row grow with their content."""
    stored = json.dumps(probe_to_dict(a_probe()))

    assert "a very long document" not in stored
    assert len(stored) < 2000


def test_no_response_stays_None_rather_than_becoming_zero():
    """`None` means no response. `0` would mean a response of zero."""
    probe = ProbeResult(
        site_url="https://x.example",
        llms_txt=Surface(url="https://x.example/llms.txt", state=SurfaceState.UNREACHABLE),
    )

    restored = probe_from_dict(probe_to_dict(probe))

    assert restored.llms_txt.status is None


def test_a_surface_that_was_never_probed_stays_absent_from_the_list():
    """A missing surface is not an unreachable one."""
    probe = ProbeResult(site_url="https://x.example")

    restored = probe_from_dict(probe_to_dict(probe))

    assert restored.surfaces() == []
    assert restored.agents_md is None


# -- readiness ---------------------------------------------------------------


def a_report() -> ReadinessReport:
    from app.core.components import by_key

    report = ReadinessReport(site_url="https://x.example", site_type=SiteType.CONTENT)
    report.sampled = ["https://x.example/", "https://x.example/blog/a-post"]
    report.results = [
        CheckResult(by_key("llms-txt"), CheckState.PASS, "200 text/markdown", "https://x.example/"),
        CheckResult(by_key("robots"), CheckState.FAIL, "404"),
        CheckResult(by_key("cls"), CheckState.MANUAL, "npx lighthouse"),
    ]
    return report


def test_a_report_round_trips_including_its_score():
    original = a_report()
    restored = readiness_from_dict(readiness_to_dict(original))

    assert restored.score == original.score
    assert restored.sampled == original.sampled
    assert [r.item.key for r in restored.results] == [r.item.key for r in original.results]
    assert [r.state for r in restored.results] == [r.state for r in original.results]
    assert [r.detail for r in restored.results] == [r.detail for r in original.results]


def test_the_sample_survives_so_a_stored_score_still_carries_its_evidence():
    """A score without its sample cannot be compared with another score.

    That was true when the number was computed live and it is more true now that
    it is read from a row taken hours ago.
    """
    restored = readiness_from_dict(readiness_to_dict(a_report()))

    assert "read 2 page(s)" in restored.summary()


def test_a_component_is_referenced_by_key_not_copied():
    """A registry edit must not disagree with thousands of stored rows."""
    stored = readiness_to_dict(a_report())

    assert stored["results"][0]["key"] == "llms-txt"
    assert "title" not in stored["results"][0]
    assert "verify" not in stored["results"][0]


def test_a_retired_component_is_dropped_rather_than_faked():
    """A key that no longer resolves means the tool stopped running that check.

    Dropping it removes it from both halves of the score's fraction, which is the
    honest outcome. Inventing a placeholder component would keep scoring a check
    that no longer exists.
    """
    stored = readiness_to_dict(a_report())
    stored["results"].append(
        {"key": "a-check-we-deleted", "state": "pass", "detail": "", "url": ""}
    )

    restored = readiness_from_dict(stored)

    assert [r.item.key for r in restored.results] == ["llms-txt", "robots", "cls"]


# -- tech --------------------------------------------------------------------


def a_tech() -> TechProfile:
    return TechProfile(
        site_url="https://x.example",
        platform=Platform.WORDPRESS,
        platform_evidence="generator meta tag",
        endpoints=[Detection(name="WP REST", evidence="200 application/json", url="/wp-json")],
        notes=["A note."],
        technologies=["WordPress", "WP Rocket"],
        ecommerce_tech=[],
    )


def test_tech_round_trips_including_what_decides_site_type():
    """`sells` picks the site type, which picks which components apply."""
    original = a_tech()
    restored = tech_from_dict(tech_to_dict(original))

    assert restored.platform is original.platform
    assert restored.sells == original.sells
    assert restored.platform_evidence == original.platform_evidence
    assert [d.name for d in restored.endpoints] == [d.name for d in original.endpoints]
    assert restored.technologies == original.technologies


def test_an_ecommerce_platform_still_sells_after_a_round_trip():
    shop = TechProfile(site_url="https://s.example", platform=Platform.SHOPIFY)

    assert tech_from_dict(tech_to_dict(shop)).sells is True


def test_an_unknown_platform_survives_as_unknown():
    """Not as a guess, and not as a crash."""
    restored = tech_from_dict(tech_to_dict(TechProfile(site_url="https://x.example")))

    assert restored.platform is Platform.UNKNOWN
    assert restored.sells is False
