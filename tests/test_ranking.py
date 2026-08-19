"""Importance scoring and Optional classification."""

from __future__ import annotations

from app.core.models import PageEntry
from app.core.ranking import (
    effective_depth,
    importance_score,
    is_contact_page,
    is_optional_page,
    score_breakdown,
    url_to_section,
)


def test_weights_sum_to_one(page: PageEntry) -> None:
    from app.core.ranking import (
        WEIGHT_CONTENT,
        WEIGHT_DEPTH,
        WEIGHT_INLINKS,
        WEIGHT_LINK_SCORE,
    )

    total = WEIGHT_LINK_SCORE + WEIGHT_INLINKS + WEIGHT_DEPTH + WEIGHT_CONTENT
    assert abs(total - 1.0) < 1e-9
    assert max(WEIGHT_LINK_SCORE, WEIGHT_INLINKS, WEIGHT_DEPTH, WEIGHT_CONTENT) == WEIGHT_LINK_SCORE


def test_breakdown_terms_sum_to_the_score(page: PageEntry) -> None:
    """The source dropped link_score between stages, costing 40% of the weighting.

    score_breakdown exists so a zeroed signal is visible rather than silent.
    """
    breakdown = score_breakdown(page)

    assert breakdown["link_score"] == 60 * 0.4
    assert abs(importance_score(page) - sum(breakdown.values())) < 1e-9


def test_losing_link_score_costs_40_percent_of_the_weighting(page: PageEntry) -> None:
    """The exact regression: a stage that forgets link_score zeroes the top term."""
    stripped = page.with_(link_score=0)

    assert score_breakdown(stripped)["link_score"] == 0.0
    assert importance_score(page) - importance_score(stripped) == 60 * 0.4


def test_unknown_depth_falls_back_to_url_path_depth() -> None:
    """The source defaulted unknown depth to 0, so every crawled page looked like
    the homepage: `## Optional` was always empty and every page scored a constant."""
    unknown = PageEntry(url="https://example.com/a/b/c/d/e")

    assert unknown.crawl_depth == -1
    assert effective_depth(unknown) == 5


def test_known_depth_wins_over_the_url_fallback() -> None:
    page = PageEntry(url="https://example.com/a/b/c/d/e", crawl_depth=1)
    assert effective_depth(page) == 1


def test_optional_requires_both_depth_and_low_importance() -> None:
    deep_and_weak = PageEntry(url="https://e.com/x", crawl_depth=5, unique_inlinks=1)
    deep_but_linked = PageEntry(
        url="https://e.com/y", crawl_depth=5, unique_inlinks=40, link_score=70
    )
    shallow_and_weak = PageEntry(url="https://e.com/z", crawl_depth=1, unique_inlinks=0)

    assert is_optional_page(deep_and_weak)
    assert not is_optional_page(deep_but_linked)
    assert not is_optional_page(shallow_and_weak)


def test_optional_fires_for_a_crawled_page_with_no_link_graph() -> None:
    """The regression that made `## Optional` dead in the source's crawl mode."""
    crawled = PageEntry(url="https://example.com/blog/tag/ci/page/7")

    assert crawled.crawl_depth == -1
    assert crawled.link_score == 0
    assert is_optional_page(crawled)


def test_crawled_pages_do_not_all_score_the_same() -> None:
    """In the source every crawl-mode page scored exactly 25, so ranking was inert."""
    shallow = PageEntry(url="https://example.com/pricing", word_count=800)
    deep = PageEntry(url="https://example.com/a/b/c/d/e", word_count=120)

    assert importance_score(shallow) > importance_score(deep)


def test_contact_detection_matches_url_or_title() -> None:
    assert is_contact_page(PageEntry(url="https://e.com/contact"))
    assert is_contact_page(PageEntry(url="https://e.com/support/help-centre"))
    assert is_contact_page(PageEntry(url="https://e.com/x", title="Store Locator"))
    assert not is_contact_page(PageEntry(url="https://e.com/docs/api", title="API Reference"))


def test_url_to_section_uses_the_first_path_segment() -> None:
    assert url_to_section("https://e.com/docs/api/auth") == "Docs"
    assert url_to_section("https://e.com/release-notes/v2") == "Release Notes"
    assert url_to_section("https://e.com/") == "Main"
