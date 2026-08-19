"""Assembly of llms.txt and llms-full.txt, per the llmstxt.org spec.

Output shape is preserved from the source tool. Three things changed:

1. The timestamp no longer crashes. The source called
   `datetime.now(datetime.timezone.utc)` while importing only the `datetime`
   *class*, so every generation raised AttributeError before returning anything.
   The date is now injected instead of read from the clock, which also makes the
   golden-file test deterministic.
2. Sections survive a rebuild. The source re-derived sections from URL paths on
   every regenerate, so one unchecked page collapsed the LLM's semantic grouping
   back into raw path buckets and discarded every section description.
3. llms-full.txt has a size budget. The source concatenated every page's markdown
   with no cap, which is the file most likely to be handed to a model with a
   context limit.
"""

from __future__ import annotations

from datetime import date

from app.core.models import GenerationResult, PageEntry, Section
from app.core.ranking import (
    PATTERN_CATALOG,
    PINNED_FIRST,
    PINNED_LAST,
    is_contact_page,
    is_optional_page,
    sort_by_importance,
    template_order,
    url_to_section,
)
from app.core.text import domain_of

# Default ceiling for llms-full.txt. Roughly 1M characters ~ 250k tokens.
DEFAULT_FULL_MAX_CHARS = 1_000_000

# A section holding fewer pages than this is not a section, it is a link with a
# heading on top.
MIN_SECTION_PAGES = 2

# Above this share of one-page sections, the URL structure is telling us nothing and
# grouping by it produces an index worse than no index at all.
FLAT_SITE_SINGLETON_RATIO = 0.6

# Where consolidated singletons go. Deliberately plain: the LLM triage stage
# replaces this grouping entirely when a key is configured.
CATCH_ALL_SECTION = "Pages"


def split_optional(pages: list[PageEntry]) -> tuple[list[PageEntry], list[PageEntry]]:
    """Partition into main pages and Optional pages."""
    main = [p for p in pages if not is_optional_page(p)]
    optional = [p for p in pages if is_optional_page(p)]
    return main, optional


def group_by_url(pages: list[PageEntry]) -> tuple[list[Section], list[PageEntry]]:
    """Deterministic grouping by first path segment. The no-LLM fallback."""
    main, optional = split_optional(pages)

    buckets: dict[str, list[PageEntry]] = {}
    for page in main:
        name = "Contact" if is_contact_page(page) else url_to_section(page.url)
        buckets.setdefault(name, []).append(page)

    sections = [
        Section(name=name, description="", pages=sort_by_importance(pages_in))
        for name, pages_in in buckets.items()
    ]
    return consolidate_singletons(sections), sort_by_importance(optional)


def consolidate_singletons(
    sections: list[Section], min_pages: int = MIN_SECTION_PAGES
) -> list[Section]:
    """Merge one-page sections when the URL structure is carrying no signal.

    Flat sites -- WordPress, most Shopify themes -- put every page one level deep, so
    the first path segment is the page's own slug. Grouping by it emits one H2 per
    page ("## Seo Gold Coast", one link), which is worse for an LLM to navigate than
    a single honest list. Measured on prosperitymedia.com.au: 12 pages produced 12
    sections.

    Only triggers when most sections are singletons. A site with a genuine structure
    and one or two small corners keeps them.
    """
    if len(sections) < 3:
        return sections

    singletons = [s for s in sections if len(s.pages) < min_pages and s.name != "Contact"]
    if len(singletons) / len(sections) < FLAT_SITE_SINGLETON_RATIO:
        return sections

    kept = [s for s in sections if s not in singletons]
    merged = [page for s in singletons for page in s.pages]
    if not merged:
        return sections

    existing = next((s for s in kept if s.name == CATCH_ALL_SECTION), None)
    if existing:
        existing.pages = sort_by_importance(existing.pages + merged)
    else:
        kept.append(Section(name=CATCH_ALL_SECTION, pages=sort_by_importance(merged)))
    return kept


def order_sections(sections: list[Section], pattern: str) -> list[Section]:
    """Template order first, then anything else alphabetically, Contact last.

    "Optional" is rendered separately by `render_llmstxt` and is dropped here if a
    caller has supplied it as a normal section.
    """
    order = template_order(pattern)
    pinned = PINNED_FIRST + PINNED_LAST
    by_name = {s.name: s for s in sections if s.name != "Optional" and s.pages}

    first = [by_name.pop(n) for n in PINNED_FIRST if n in by_name]
    templated = [by_name.pop(n) for n in order if n in by_name and n not in pinned]
    remaining = [by_name.pop(n) for n in sorted(by_name) if n not in pinned]
    last = [by_name[n] for n in PINNED_LAST if n in by_name]

    ordered = first + templated + remaining + last
    for position, section in enumerate(ordered):
        section.position = position
    return ordered


def apply_manual_order(sections: list[Section], order: list[str]) -> list[Section]:
    """Reorder by an explicit list of section names, appending anything unlisted.

    Section reordering existed in the original Streamlit UI and was dropped in the
    Flask migration. Keeping it as a pure function means the UI can offer it again
    without the renderer knowing anything about the UI.
    """
    by_name = {s.name: s for s in sections}
    ordered = [by_name.pop(name) for name in order if name in by_name]
    ordered.extend(by_name[name] for name in sorted(by_name))
    for position, section in enumerate(ordered):
        section.position = position
    return ordered


def _link_line(page: PageEntry) -> str:
    return f"- [{page.display_title}]({page.url}): {page.description}"


def render_llmstxt(
    site_url: str,
    site_name: str,
    site_summary: str,
    sections: list[Section],
    optional: list[PageEntry],
    pattern: str = PATTERN_CATALOG,
    generated_on: date | None = None,
    include_full_reference: bool = True,
) -> str:
    """Build the llms.txt body.

    `generated_on` is injected rather than read from the clock so the output is
    reproducible and the golden-file test is stable.
    """
    domain = domain_of(site_url)
    lines: list[str] = [f"# {site_name}\n"]

    # The blockquote is required by the spec, so there is always a fallback.
    lines.append(
        f"> {site_summary}\n" if site_summary else f"> Official website content for {domain}.\n"
    )

    if include_full_reference:
        base = site_url.rstrip("/")
        lines.append(f"For full page content, see [{domain}/llms-full.txt]({base}/llms-full.txt)")

    for section in order_sections(sections, pattern):
        lines.append(f"\n## {section.name}")
        lines.append(f"\n{section.description}\n" if section.description else "")
        lines.extend(_link_line(page) for page in section.pages)

    if optional:
        lines.append("\n## Optional\n")
        lines.extend(_link_line(page) for page in optional)

    stamp = (generated_on or date.today()).isoformat()
    lines.append(
        f"\n---\n*Generated {stamp}. Recommend reviewing quarterly or after major site changes.*\n"
    )

    return "\n".join(lines) + "\n"


def render_llms_full(
    site_name: str,
    pages: list[PageEntry],
    max_chars: int = DEFAULT_FULL_MAX_CHARS,
) -> str:
    """Build llms-full.txt, most important page first, within a character budget.

    Pages are emitted in importance order so that a truncated file keeps the pages
    that matter. Truncation is stated in the file rather than left silent.
    """
    with_content = [p for p in pages if p.markdown]
    if not with_content:
        return ""

    lines = [f"# {site_name}\n"]
    used = len(lines[0])
    omitted = 0

    for page in sort_by_importance(with_content):
        block = f"\n---\n\n## {page.display_title}\n\nSource: {page.url}\n\n{page.markdown}\n"
        if used + len(block) > max_chars:
            omitted += 1
            continue
        lines.append(block)
        used += len(block)

    if omitted:
        lines.append(
            f"\n---\n\n*{omitted} lower-priority page(s) omitted to keep this file under "
            f"{max_chars:,} characters.*\n"
        )

    return "".join(lines)


def render_combined(llmstxt: str, llms_full: str) -> str:
    """Single-file mode: the index followed by the full content."""
    if not llms_full:
        return llmstxt
    return f"{llmstxt}\n\n{llms_full}"


def build_result(
    site_url: str,
    site_name: str,
    site_summary: str,
    sections: list[Section],
    optional: list[PageEntry],
    pattern: str,
    pages_total: int,
    generated_on: date | None = None,
    generate_full: bool = True,
    full_max_chars: int = DEFAULT_FULL_MAX_CHARS,
) -> GenerationResult:
    """Assemble both files into a `GenerationResult`. Validation is applied separately."""
    ordered = order_sections(sections, pattern)
    llmstxt = render_llmstxt(
        site_url, site_name, site_summary, ordered, optional, pattern, generated_on
    )

    llms_full = ""
    if generate_full:
        included = [p for s in ordered for p in s.pages] + list(optional)
        llms_full = render_llms_full(site_name, included, full_max_chars)

    return GenerationResult(
        site_url=site_url,
        site_name=site_name,
        site_summary=site_summary,
        pattern=pattern,
        sections=ordered,
        optional=optional,
        llmstxt=llmstxt,
        llms_full=llms_full,
        pages_total=pages_total,
    )
