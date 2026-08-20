"""Reconciling the three page caps that can be in play at once."""

from __future__ import annotations

from app.scrape.preflight import effective_page_cap
from app.scrape.sizing import assess


def size_for(sitemap: int, indexed: int | None = None):
    return assess(
        "https://example.com", [f"https://example.com/p{i}" for i in range(sitemap)], indexed
    )


def test_an_explicit_request_always_wins():
    """The size check advises. It does not overrule a human who typed a number."""
    size = size_for(60_000)
    assert size.recommended_max_pages == 1_500
    assert effective_page_cap(size, requested=5_000, configured_default=500) == 5_000


def test_small_site_takes_everything_it_has():
    size = size_for(40)
    assert effective_page_cap(size, requested=None, configured_default=500) == 40


def test_small_site_with_no_sitemap_falls_back_to_the_configured_default():
    size = assess("https://example.com", [], indexed_estimate=None)
    assert effective_page_cap(size, requested=None, configured_default=500) == 500


def test_large_site_takes_the_tighter_of_suggestion_and_default():
    size = size_for(5_000)
    assert size.recommended_max_pages == 1_000
    assert effective_page_cap(size, requested=None, configured_default=500) == 500
    assert effective_page_cap(size, requested=None, configured_default=2_000) == 1_000


def test_zero_is_not_treated_as_a_request():
    size = size_for(5_000)
    assert effective_page_cap(size, requested=0, configured_default=500) == 500
