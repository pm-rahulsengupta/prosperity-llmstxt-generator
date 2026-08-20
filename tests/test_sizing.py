"""Pre-flight size estimation. Offline: the network call is one thin function."""

from __future__ import annotations

from app.scrape.sizing import (
    assess,
    classify,
    count_html_urls,
    parse_results_count,
)


def urls(n: int, prefix: str = "https://example.com/p") -> list[str]:
    return [f"{prefix}{i}" for i in range(n)]


def test_assets_are_not_counted_as_pages():
    listed = [
        "https://example.com/",
        "https://example.com/about",
        "https://example.com/logo.png",
        "https://example.com/brochure.pdf",
        "https://example.com/feed.xml",
    ]
    total, html = count_html_urls(listed)
    assert (total, html) == (5, 2)


def test_extensionless_and_trailing_slash_urls_count_as_pages():
    listed = [
        "https://example.com/docs/",
        "https://example.com/docs/getting-started",
        "https://example.com/2026/03/release-notes",
    ]
    assert count_html_urls(listed) == (3, 3)


def test_tiers_and_caps():
    assert classify(12) == ("small", 0)
    assert classify(400) == ("medium", 400)
    assert classify(5_000) == ("large", 1_000)
    assert classify(250_000) == ("huge", 1_500)


def test_small_site_gets_no_cap():
    estimate = assess("https://example.com", urls(40), indexed_estimate=38, indexed_source="dfs")
    assert estimate.tier == "small"
    assert estimate.recommended_max_pages == 0
    assert estimate.warnings == []
    assert estimate.needs_review is False


def test_index_far_larger_than_sitemap_warns_about_orphans():
    estimate = assess(
        "https://example.com", urls(100), indexed_estimate=4_000, indexed_source="dfs"
    )
    assert any("orphan pages" in w for w in estimate.warnings)
    # The bigger of the two counts drives the budget, not the sitemap alone.
    assert estimate.tier == "large"
    assert estimate.recommended_max_pages == 1_000


def test_sitemap_far_larger_than_index_warns_about_thin_pages():
    estimate = assess("https://example.com", urls(900), indexed_estimate=60, indexed_source="dfs")
    assert any("not being kept" in w for w in estimate.warnings)


def test_small_absolute_gap_does_not_warn():
    """20 sitemapped vs 5 indexed is a 4x ratio but only 15 pages. Not a finding."""
    estimate = assess("https://example.com", urls(20), indexed_estimate=5, indexed_source="dfs")
    assert not any("not being kept" in w for w in estimate.warnings)


def test_missing_credentials_are_reported_not_hidden():
    estimate = assess("https://example.com", urls(30))
    assert estimate.indexed_source == "unavailable"
    assert any("DataForSEO credentials" in w for w in estimate.warnings)


def test_empty_sitemap_says_so():
    estimate = assess("https://example.com", [], indexed_estimate=500, indexed_source="dfs")
    assert any("No crawlable URLs" in w for w in estimate.warnings)
    assert estimate.best_count == 500


def test_huge_site_demands_review():
    estimate = assess("https://example.com", urls(60_000), indexed_estimate=58_000)
    assert estimate.tier == "huge"
    assert estimate.needs_review is True
    assert any("real money" in w for w in estimate.warnings)


def test_parse_results_count_reads_dataforseo_shape():
    body = {
        "tasks": [
            {"result": [{"keyword": "site:example.com", "se_results_count": 12_400, "items": []}]}
        ]
    }
    assert parse_results_count(body) == 12_400


def test_parse_results_count_survives_an_error_payload():
    assert parse_results_count({"tasks": [{"result": None, "status_code": 40501}]}) is None
    assert parse_results_count({}) is None
