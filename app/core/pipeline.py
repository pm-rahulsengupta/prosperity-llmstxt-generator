"""The deterministic generation pipeline: entries in, GenerationResult out.

This path takes no network calls and needs no LLM key. Every LLM stage in
`app/llm/` is an optional enhancement layered on top of it, which is what keeps
the tool usable when a key is missing, rate-limited or refused -- the source
degraded silently instead, and the fallback was invisible in the output.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from app.core.csv_source import (
    FilterReport,
    deduplicate,
    filter_near_duplicates,
    filter_thin_content,
)
from app.core.models import GenerationResult, PageEntry, Section
from app.core.ranking import PATTERN_CATALOG
from app.core.render import DEFAULT_FULL_MAX_CHARS, build_result, group_by_url
from app.core.text import domain_of, truncate_text, truncate_title
from app.core.validate import validate


@dataclass(slots=True)
class FilterOptions:
    dedup: bool = True
    near_duplicates: bool = False
    near_duplicate_threshold: float = 90.0
    thin_content: bool = False
    min_word_count: int = 50


@dataclass(slots=True)
class GenerateOptions:
    pattern: str = PATTERN_CATALOG
    generate_full: bool = True
    full_max_chars: int = DEFAULT_FULL_MAX_CHARS
    site_name: str = ""
    site_summary: str = ""
    generated_on: date | None = None
    filters: FilterOptions = field(default_factory=FilterOptions)


def apply_filters(
    entries: list[PageEntry], options: FilterOptions
) -> tuple[list[PageEntry], list[FilterReport]]:
    """Run the filter chain in order, returning what each stage removed.

    Order matters: dedup first so near-duplicate and thin-content counts describe
    real pages rather than copies of one another.
    """
    reports: list[FilterReport] = []

    if options.dedup:
        entries, report = deduplicate(entries)
        reports.append(report)
    if options.near_duplicates:
        entries, report = filter_near_duplicates(entries, options.near_duplicate_threshold)
        reports.append(report)
    if options.thin_content:
        entries, report = filter_thin_content(entries, options.min_word_count)
        reports.append(report)

    return entries, reports


def apply_fallback_copy(entries: list[PageEntry]) -> list[PageEntry]:
    """Fill titles and descriptions without an LLM.

    Uses the page's own metadata where it exists, trimmed to the spec's shape, and
    derives from the URL where it does not. This is what the output looks like with
    no API key at all -- serviceable, not great, and never empty.
    """
    from app.core.text import description_from_url, title_from_url

    filled: list[PageEntry] = []
    for entry in entries:
        title = truncate_title(entry.display_title) or title_from_url(entry.url)
        description = truncate_text(entry.description) or description_from_url(entry.url)
        filled.append(entry.with_(title=title, description=description))
    return filled


def generate(
    site_url: str,
    entries: list[PageEntry],
    options: GenerateOptions | None = None,
    sections: list[Section] | None = None,
    optional: list[PageEntry] | None = None,
) -> tuple[GenerationResult, list[FilterReport]]:
    """Produce a complete `GenerationResult` from page entries.

    `sections`/`optional` let a caller supply an LLM-derived grouping. When they are
    omitted the deterministic URL grouping is used, so this function is the whole
    tool in the no-key case.
    """
    options = options or GenerateOptions()
    total = len(entries)

    entries, reports = apply_filters(entries, options.filters)
    entries = apply_fallback_copy(entries)

    if sections is None:
        sections, derived_optional = group_by_url(entries)
        optional = derived_optional if optional is None else optional
    optional = optional or []

    result = build_result(
        site_url=site_url,
        site_name=options.site_name or domain_of(site_url),
        site_summary=options.site_summary,
        sections=sections,
        optional=optional,
        pattern=options.pattern,
        pages_total=total,
        generated_on=options.generated_on,
        generate_full=options.generate_full,
        full_max_chars=options.full_max_chars,
    )
    result.issues = validate(result.llmstxt, entries)
    return result, reports


def rebuild(
    result: GenerationResult,
    excluded_urls: set[str],
    site_name: str | None = None,
    site_summary: str | None = None,
    section_order: list[str] | None = None,
    generated_on: date | None = None,
) -> GenerationResult:
    """Re-render after user edits, preserving the existing section assignments.

    The source re-derived sections from URL paths here, so unchecking a single page
    collapsed the LLM's semantic grouping into raw path buckets and discarded every
    section description. It also rebuilt only llms.txt, leaving excluded pages in
    llms-full.txt and therefore in the combined download.
    """
    from app.core.render import apply_manual_order

    kept_sections = [
        Section(
            name=s.name,
            description=s.description,
            pages=[p for p in s.pages if p.url not in excluded_urls],
            position=s.position,
        )
        for s in result.sections
    ]
    kept_sections = [s for s in kept_sections if s.pages]
    if section_order:
        kept_sections = apply_manual_order(kept_sections, section_order)

    kept_optional = [p for p in result.optional if p.url not in excluded_urls]

    rebuilt = build_result(
        site_url=result.site_url,
        site_name=site_name if site_name is not None else result.site_name,
        site_summary=site_summary if site_summary is not None else result.site_summary,
        sections=kept_sections,
        optional=kept_optional,
        pattern=result.pattern,
        pages_total=result.pages_total,
        generated_on=generated_on,
        generate_full=bool(result.llms_full),
    )
    included = [p for s in rebuilt.sections for p in s.pages] + rebuilt.optional
    rebuilt.issues = validate(rebuilt.llmstxt, included)
    return rebuilt
