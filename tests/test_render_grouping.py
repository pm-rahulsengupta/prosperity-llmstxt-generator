"""Section grouping for the no-LLM path, including flat-site consolidation."""

from __future__ import annotations

from app.core.models import PageEntry, Section
from app.core.render import (
    CATCH_ALL_SECTION,
    apply_manual_order,
    consolidate_singletons,
    group_by_url,
    order_sections,
)
from app.core.ranking import PATTERN_CATALOG


def _pages(*urls: str) -> list[PageEntry]:
    return [PageEntry(url=u, title=u.rsplit("/", 1)[-1] or "Home", crawl_depth=1) for u in urls]


def test_a_structured_site_keeps_its_structure() -> None:
    sections, _ = group_by_url(
        _pages(
            "https://e.com/docs/a",
            "https://e.com/docs/b",
            "https://e.com/docs/c",
            "https://e.com/blog/x",
            "https://e.com/blog/y",
        )
    )
    by_name = {s.name: len(s.pages) for s in sections}

    assert by_name == {"Docs": 3, "Blog": 2}


def test_a_flat_site_collapses_into_one_section() -> None:
    """Measured on prosperitymedia.com.au: 12 pages produced 12 one-link sections."""
    sections, _ = group_by_url(
        _pages(
            "https://e.com/seo-melbourne",
            "https://e.com/seo-gold-coast",
            "https://e.com/b2b-seo",
            "https://e.com/digital-pr-agency",
            "https://e.com/about",
        )
    )

    assert len(sections) == 1
    assert sections[0].name == CATCH_ALL_SECTION
    assert len(sections[0].pages) == 5


def test_consolidation_leaves_contact_alone() -> None:
    """Contact is pinned and meaningful even with a single page in it."""
    sections, _ = group_by_url(
        _pages(
            "https://e.com/seo-melbourne",
            "https://e.com/seo-gold-coast",
            "https://e.com/b2b-seo",
            "https://e.com/contact",
        )
    )
    by_name = {s.name for s in sections}

    assert "Contact" in by_name
    assert CATCH_ALL_SECTION in by_name


def test_a_mostly_structured_site_keeps_its_small_corners() -> None:
    """One or two thin sections are not evidence the URL structure is meaningless."""
    sections = [
        Section("Docs", pages=_pages("https://e.com/docs/a", "https://e.com/docs/b")),
        Section("Blog", pages=_pages("https://e.com/blog/a", "https://e.com/blog/b")),
        Section("Pricing", pages=_pages("https://e.com/pricing")),
    ]
    result = consolidate_singletons(sections)

    assert {s.name for s in result} == {"Docs", "Blog", "Pricing"}


def test_consolidation_needs_at_least_three_sections() -> None:
    sections = [
        Section("A", pages=_pages("https://e.com/a")),
        Section("B", pages=_pages("https://e.com/b")),
    ]
    assert len(consolidate_singletons(sections)) == 2


def test_template_sections_come_before_the_rest() -> None:
    sections = [
        Section("Zebra", pages=_pages("https://e.com/z")),
        Section("Guides", pages=_pages("https://e.com/g")),
        Section("Apple", pages=_pages("https://e.com/a")),
    ]
    ordered = [s.name for s in order_sections(sections, PATTERN_CATALOG)]

    assert ordered[0] == "Guides", "Guides is in the catalog template"
    assert ordered[1:] == ["Apple", "Zebra"], "the rest sort alphabetically"


def test_homepage_section_is_pinned_first() -> None:
    sections = [
        Section("Guides", pages=_pages("https://e.com/g")),
        Section("Main", pages=_pages("https://e.com/")),
    ]
    assert [s.name for s in order_sections(sections, PATTERN_CATALOG)][0] == "Main"


def test_contact_is_pinned_last() -> None:
    sections = [
        Section("Contact", pages=_pages("https://e.com/contact")),
        Section("Guides", pages=_pages("https://e.com/g")),
    ]
    assert [s.name for s in order_sections(sections, PATTERN_CATALOG)][-1] == "Contact"


def test_manual_order_wins_and_appends_the_unlisted() -> None:
    """Section reordering, dropped in the source's Flask migration."""
    sections = [
        Section("Docs", pages=_pages("https://e.com/d")),
        Section("Blog", pages=_pages("https://e.com/b")),
        Section("API", pages=_pages("https://e.com/a")),
    ]
    ordered = apply_manual_order(sections, ["Blog", "Docs"])

    assert [s.name for s in ordered] == ["Blog", "Docs", "API"]
    assert [s.position for s in ordered] == [0, 1, 2]


def test_empty_sections_are_dropped_from_the_output() -> None:
    sections = [
        Section("Docs", pages=_pages("https://e.com/d")),
        Section("Empty", pages=[]),
    ]
    assert [s.name for s in order_sections(sections, PATTERN_CATALOG)] == ["Docs"]
