"""Pre-flight size estimation. Offline: the network call is one thin function."""

from __future__ import annotations

from app.scrape.sizing import (
    HTTP_STATUS_REASONS,
    CountFailure,
    IndexedCount,
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
    result = parse_results_count(body)
    assert result.count == 12_400
    assert result.reason is CountFailure.OK
    assert not result.failed


def test_an_ip_rejection_is_reported_as_an_ip_rejection():
    """The live failure: HTTP 200, envelope 20000 Ok, refusal one level down.

    Reading only the count is what made this present as "credentials are not
    configured" and sent a real IP whitelist problem on a detour through the
    credentials.
    """
    body = {
        "status_code": 20000,
        "status_message": "Ok.",
        "tasks": [
            {
                "status_code": 40207,
                "status_message": "Access denied. Your IP is not whitelisted.",
                "result": None,
            }
        ],
    }
    result = parse_results_count(body)

    assert result.failed
    assert result.reason is CountFailure.NOT_WHITELISTED
    assert "not whitelisted" in result.explain().lower()
    assert "credentials are fine" in result.explain()
    # And it must NOT blame configuration.
    assert "not configured" not in result.explain()


def test_task_status_codes_map_to_distinct_causes():
    def reason_for(status: int) -> CountFailure:
        return parse_results_count(
            {"tasks": [{"status_code": status, "status_message": "x", "result": None}]}
        ).reason

    assert reason_for(40100) is CountFailure.AUTH_FAILED
    assert reason_for(40200) is CountFailure.OUT_OF_CREDITS
    assert reason_for(40207) is CountFailure.NOT_WHITELISTED
    assert reason_for(40209) is CountFailure.RATE_LIMITED
    # An unmapped refusal is still a refusal, not a success.
    assert reason_for(49999) is CountFailure.API_ERROR


def test_an_empty_but_valid_response_is_not_an_error():
    """A domain Google indexes nothing for is a real answer, not a failure to ask."""
    result = parse_results_count({"tasks": [{"status_code": 20000, "result": []}]})
    assert result.failed
    assert result.reason is CountFailure.NO_RESULTS
    assert "not indexed" in result.explain()


def test_http_level_refusals_are_not_reported_as_transport_failures():
    """A 401 is a rejected password, not an unreachable server.

    Caught in production: `raise_for_status()` sent every HTTP error down the
    transport path, so a wrong password read as "could not reach DataForSEO" --
    the same wrong-cause failure this module was rewritten to eliminate.
    """
    assert HTTP_STATUS_REASONS[401] is CountFailure.AUTH_FAILED
    assert HTTP_STATUS_REASONS[402] is CountFailure.OUT_OF_CREDITS
    assert HTTP_STATUS_REASONS[429] is CountFailure.RATE_LIMITED

    message = IndexedCount(reason=CountFailure.AUTH_FAILED, detail="HTTP 401").explain()
    assert "rejected the credentials" in message
    assert "could not reach" not in message


def test_assess_reports_the_real_reason_not_a_guess():
    estimate = assess(
        "https://example.com",
        urls(30),
        indexed=IndexedCount(reason=CountFailure.NOT_WHITELISTED, detail="40207 Access denied."),
    )
    warning = " ".join(estimate.warnings)
    assert "not whitelisted" in warning.lower()
    assert "credentials are not configured" not in warning


def test_assess_still_blames_configuration_when_that_is_the_truth():
    estimate = assess(
        "https://example.com", urls(30), indexed=IndexedCount(reason=CountFailure.NO_CREDENTIALS)
    )
    assert any("credentials are not configured" in w for w in estimate.warnings)
