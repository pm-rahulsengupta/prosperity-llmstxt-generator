"""Screaming Frog import and the dedup/quality filters."""

from __future__ import annotations

from app.core.csv_source import (
    deduplicate,
    filter_near_duplicates,
    filter_thin_content,
    parse_screaming_frog_csv,
)


def test_rejects_non_200_non_html_and_non_indexable(sf_csv: str) -> None:
    urls = {e.url for e in parse_screaming_frog_csv(sf_csv)}

    assert "https://example.com/old-pricing" not in urls, "301 must be rejected"
    assert "https://example.com/missing" not in urls, "404 must be rejected"
    assert "https://example.com/whitepaper.pdf" not in urls, "PDF must be rejected"
    assert "https://example.com/search?q=ci" not in urls, "noindex must be rejected"
    assert "https://example.com/" in urls


def test_carries_every_ranking_signal(sf_csv: str) -> None:
    """The source dropped link_score and word_count between stages.

    That silently zeroed the 40%-weighted term. Assert the whole record survives
    parsing so the regression is caught here rather than in a client deliverable.
    """
    entries = {e.url: e for e in parse_screaming_frog_csv(sf_csv)}
    home = entries["https://example.com/"]

    assert home.link_score == 100
    assert home.unique_inlinks == 95
    assert home.word_count == 620
    assert home.crawl_depth == 0
    assert home.text_ratio == 42.10
    assert home.content_hash == "h000"


def test_max_urls_truncates_in_row_order(sf_csv: str) -> None:
    assert len(parse_screaming_frog_csv(sf_csv, max_urls=3)) == 3


def test_empty_input_is_not_an_error() -> None:
    assert parse_screaming_frog_csv("") == []
    assert parse_screaming_frog_csv("Address,Status Code\n") == []


def test_deduplicate_drops_page_pointing_at_a_canonical_that_is_present(sf_csv: str) -> None:
    """The utm variant canonicalises to /docs/quickstart, which is in the export.

    The source kept both. Its docstring and README said it dropped the variant.
    """
    kept, report = deduplicate(parse_screaming_frog_csv(sf_csv))
    urls = {e.url for e in kept}

    assert "https://example.com/docs/quickstart?utm_source=twitter" not in urls
    assert "https://example.com/docs/quickstart" in urls
    assert report.count >= 1


def test_deduplicate_keeps_page_whose_canonical_is_absent() -> None:
    """Dropping it would remove the content from the output entirely."""
    from app.core.models import PageEntry

    entries = [PageEntry(url="https://a.example/x", canonical="https://a.example/elsewhere")]
    kept, report = deduplicate(entries)

    assert [e.url for e in kept] == ["https://a.example/x"]
    assert report.count == 0


def test_filter_thin_content_keeps_pages_with_no_word_count() -> None:
    from app.core.models import PageEntry

    entries = [
        PageEntry(url="https://a.example/thin", word_count=18),
        PageEntry(url="https://a.example/unknown", word_count=0),
        PageEntry(url="https://a.example/full", word_count=900),
    ]
    kept, report = filter_thin_content(entries, min_word_count=50)

    assert {e.url for e in kept} == {"https://a.example/unknown", "https://a.example/full"}
    assert report.count == 1


def test_near_duplicate_filter_drops_the_print_variant(sf_csv: str) -> None:
    kept, report = filter_near_duplicates(parse_screaming_frog_csv(sf_csv), 90.0)

    assert "https://example.com/docs/quickstart/print" not in {e.url for e in kept}
    assert report.count == 1


def test_near_duplicate_filter_protects_heavily_linked_pages() -> None:
    """A near-duplicate hub is usually the canonical; its twin is the thin one."""
    from app.core.models import PageEntry

    entries = [
        PageEntry(url="https://a.example/hub", unique_inlinks=500, closest_similarity=99.0),
        PageEntry(url="https://a.example/twin", unique_inlinks=1, closest_similarity=99.0),
        PageEntry(url="https://a.example/other", unique_inlinks=40, closest_similarity=0.0),
    ]
    kept, _ = filter_near_duplicates(entries, 90.0)

    assert "https://a.example/hub" in {e.url for e in kept}
    assert "https://a.example/twin" not in {e.url for e in kept}
