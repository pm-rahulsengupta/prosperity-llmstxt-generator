"""robots.txt parsing, sitemap parsing and URL path-template clustering.

All offline: these are pure functions over text, which is the point.
"""

from __future__ import annotations

from app.scrape.recon import (
    RobotsInfo,
    classify_segment,
    cluster_urls,
    extension_counts,
    parse_robots,
    parse_sitemap,
    sitemap_candidates,
)

ROBOTS = """
# comment line
User-agent: *
Disallow: /search
Disallow: /cart
Crawl-delay: 2

User-agent: BadBot
Disallow: /

Sitemap: https://example.com/sitemap_index.xml
Sitemap: https://example.com/news-sitemap.xml
"""


def test_parse_robots_extracts_sitemaps_disallows_and_delay() -> None:
    info = parse_robots(ROBOTS)

    assert info.sitemaps == [
        "https://example.com/sitemap_index.xml",
        "https://example.com/news-sitemap.xml",
    ]
    assert info.disallowed == ["/search", "/cart"]
    assert info.crawl_delay == 2.0


def test_parse_robots_ignores_other_user_agent_groups() -> None:
    """A blanket Disallow aimed at BadBot must not be read as aimed at us."""
    assert "/" not in parse_robots(ROBOTS).disallowed


def test_parse_sitemap_urlset() -> None:
    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url><loc>https://example.com/</loc></url>
      <url><loc>https://example.com/pricing</loc></url>
    </urlset>"""
    pages, nested = parse_sitemap(xml)

    assert pages == ["https://example.com/", "https://example.com/pricing"]
    assert nested == []


def test_parse_sitemap_index_returns_nested_sitemaps() -> None:
    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <sitemap><loc>https://example.com/sitemap-posts.xml</loc></sitemap>
      <sitemap><loc>https://example.com/sitemap-pages.xml</loc></sitemap>
    </sitemapindex>"""
    pages, nested = parse_sitemap(xml)

    assert pages == []
    assert nested == [
        "https://example.com/sitemap-posts.xml",
        "https://example.com/sitemap-pages.xml",
    ]


def test_parse_sitemap_without_a_namespace() -> None:
    pages, _ = parse_sitemap("<urlset><url><loc>https://e.com/a</loc></url></urlset>")
    assert pages == ["https://e.com/a"]


def test_parse_sitemap_survives_malformed_xml() -> None:
    assert parse_sitemap("<urlset><url>") == ([], [])
    assert parse_sitemap("") == ([], [])


def test_classify_segment() -> None:
    assert classify_segment("2026") == "{year}"
    assert classify_segment("41827") == "{id}"
    assert classify_segment("2026-08-20") == "{date}"
    assert classify_segment("how-we-scaled-ci") == "{slug}"
    assert classify_segment("docs") is None
    assert classify_segment("pricing") is None


def test_cluster_collapses_high_cardinality_segments() -> None:
    urls = [f"https://e.com/blog/post-number-{i}" for i in range(40)]
    urls += [
        "https://e.com/pricing",
        "https://e.com/docs/quickstart",
        "https://e.com/docs/concepts",
    ]
    by_template = {t.template: t for t in cluster_urls(urls)}

    assert by_template["/blog/{slug}"].count == 40
    assert "/pricing" in by_template
    assert by_template["/pricing"].count == 1


def test_cluster_keeps_low_cardinality_segments_literal() -> None:
    """Three doc pages are three pages, not a /docs/{slug} shape."""
    urls = [
        "https://e.com/docs/a",
        "https://e.com/docs/b",
        "https://e.com/docs/c",
    ]
    templates = {t.template for t in cluster_urls(urls)}

    assert templates == {"/docs/a", "/docs/b", "/docs/c"}


def test_cluster_handles_nested_variable_segments() -> None:
    urls = [
        f"https://e.com/products/{cat}/item-{i}-detail"
        for cat in ("bags", "shoes", "coats", "hats", "belts", "socks")
        for i in range(8)
    ]
    by_template = {t.template: t for t in cluster_urls(urls)}

    assert "/products/{slug}/{slug}" in by_template
    assert by_template["/products/{slug}/{slug}"].count == 48


def test_cluster_orders_by_count_descending() -> None:
    urls = [f"https://e.com/blog/post-{i}-x" for i in range(30)]
    urls += [f"https://e.com/docs/guide-{i}-y" for i in range(10)]
    templates = cluster_urls(urls)

    assert templates[0].count >= templates[-1].count
    assert templates[0].template == "/blog/{slug}"


def test_cluster_handles_the_homepage() -> None:
    templates = cluster_urls(["https://e.com/", "https://e.com"])

    assert len(templates) == 1
    assert templates[0].template == "/"
    assert templates[0].count == 2


def test_cluster_of_empty_input() -> None:
    assert cluster_urls([]) == []


def test_extension_counts_spots_assets_in_a_sitemap() -> None:
    counts = extension_counts(
        [
            "https://e.com/a.pdf",
            "https://e.com/b.pdf",
            "https://e.com/c.jpg",
            "https://e.com/docs/quickstart",
        ]
    )

    assert counts["pdf"] == 2
    assert counts["jpg"] == 1
    assert "quickstart" not in counts


def test_sitemap_candidates_prefers_robots_then_conventions() -> None:
    robots = RobotsInfo(sitemaps=["https://e.com/custom.xml"], fetched=True)
    candidates = sitemap_candidates("https://e.com", robots)

    assert candidates[0] == "https://e.com/custom.xml"
    assert "https://e.com/sitemap.xml" in candidates
