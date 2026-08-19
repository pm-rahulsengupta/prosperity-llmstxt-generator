"""Screaming Frog "Internal All" CSV import, plus the dedup and quality filters.

Screaming Frog is the only source of Link Score, Unique Inlinks and Crawl Depth --
the three signals `ranking.importance_score` is actually built on. A crawler cannot
produce them, so this import path stays first-class rather than becoming a legacy
fallback.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass

from app.core.models import PageEntry
from app.core.text import safe_float, safe_int

# Column aliases, tried in order. Matching is case-insensitive.
COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "url": ("address", "url"),
    "status_code": ("status code",),
    "content_type": ("content type",),
    "indexability": ("indexability",),
    "title": ("title 1", "title"),
    "description": ("meta description 1", "meta description", "description 1"),
    "h1": ("h1-1", "h1"),
    "word_count": ("word count",),
    "text_ratio": ("text ratio",),
    "crawl_depth": ("crawl depth",),
    "folder_depth": ("folder depth",),
    "inlinks": ("inlinks",),
    "unique_inlinks": ("unique inlinks",),
    "outlinks": ("outlinks",),
    "external_outlinks": ("external outlinks",),
    "link_score": ("link score",),
    "content_hash": ("hash",),
    "canonical": ("canonical link element 1", "canonical link element"),
    "closest_similarity": ("closest similarity match",),
    "response_time": ("response time",),
}


@dataclass(slots=True)
class FilterReport:
    """What a filter removed, so the UI can show it instead of silently shrinking."""

    stage: str
    removed: list[PageEntry]

    @property
    def count(self) -> int:
        return len(self.removed)


def parse_screaming_frog_csv(file_contents: str, max_urls: int = 0) -> list[PageEntry]:
    """Parse an Internal All export into `PageEntry` records.

    Rows are rejected outright when the status is not 200, the content type is not
    HTML, or Screaming Frog marked the URL non-indexable. `max_urls` truncates in CSV
    row order -- it is a cost guard, not a ranking, so prefer filtering the export in
    Screaming Frog over relying on it.
    """
    reader = csv.DictReader(io.StringIO(file_contents))
    if not reader.fieldnames:
        return []

    header_map = {h.lower().strip(): h for h in reader.fieldnames if h}

    def get(row: dict[str, str], key: str) -> str:
        for alias in COLUMN_ALIASES[key]:
            header = header_map.get(alias)
            if header is not None:
                value = row.get(header)
                if value:
                    return value.strip()
        return ""

    entries: list[PageEntry] = []
    for row in reader:
        url = get(row, "url")
        if not url:
            continue

        status = get(row, "status_code")
        if status and status != "200":
            continue
        content_type = get(row, "content_type")
        if content_type and "text/html" not in content_type.lower():
            continue
        # Screaming Frog qualifies this field: "Non-Indexable, noindex",
        # "Non-Indexable, Redirected", "Non-Indexable, Canonicalised". The source
        # compared for exact equality with "non-indexable", so on a real export the
        # filter matched almost nothing and noindex pages went straight into the
        # output. Match the prefix.
        indexability = get(row, "indexability")
        if indexability and indexability.lower().startswith("non-indexable"):
            continue

        # Crawl Depth is absent from some export profiles. -1 means "unknown", which
        # ranking.effective_depth resolves from the URL path rather than pretending
        # the page is the homepage.
        raw_depth = get(row, "crawl_depth")
        crawl_depth = safe_int(raw_depth) if raw_depth else -1

        entries.append(
            PageEntry(
                url=url,
                title=get(row, "title"),
                description=get(row, "description"),
                h1=get(row, "h1"),
                word_count=safe_int(get(row, "word_count")),
                text_ratio=safe_float(get(row, "text_ratio")),
                crawl_depth=crawl_depth,
                folder_depth=safe_int(get(row, "folder_depth")),
                inlinks=safe_int(get(row, "inlinks")),
                unique_inlinks=safe_int(get(row, "unique_inlinks")),
                outlinks=safe_int(get(row, "outlinks")),
                external_outlinks=safe_int(get(row, "external_outlinks")),
                link_score=safe_int(get(row, "link_score")),
                content_hash=get(row, "content_hash"),
                canonical=get(row, "canonical"),
                closest_similarity=safe_float(get(row, "closest_similarity")),
                response_time=safe_float(get(row, "response_time")),
                status_code=safe_int(status) if status else 0,
            )
        )

        if max_urls and len(entries) >= max_urls:
            break

    for i, entry in enumerate(entries):
        entry.index = i
    return entries


def deduplicate(entries: list[PageEntry]) -> tuple[list[PageEntry], FilterReport]:
    """Drop non-canonical duplicates and exact content-hash duplicates.

    The source claimed, in both its docstring and its README, that a page whose
    canonical points elsewhere is skipped. The code kept it and only dropped a
    *second* page sharing that canonical. This does what was documented, with one
    guard the original lacked: a page is dropped in favour of its canonical only if
    that canonical is actually present in the export. Otherwise dropping it would
    remove the content from the output altogether.
    """
    present = {e.url for e in entries}
    kept: list[PageEntry] = []
    removed: list[PageEntry] = []
    seen_hashes: set[str] = set()

    for entry in entries:
        canonical = entry.canonical
        if canonical and canonical != entry.url and canonical in present:
            removed.append(entry)
            continue

        if entry.content_hash:
            if entry.content_hash in seen_hashes:
                removed.append(entry)
                continue
            seen_hashes.add(entry.content_hash)

        kept.append(entry)

    return kept, FilterReport("duplicates", removed)


def filter_thin_content(
    entries: list[PageEntry], min_word_count: int = 50
) -> tuple[list[PageEntry], FilterReport]:
    """Drop pages below a word count.

    A word count of 0 means the column was absent, not that the page is empty, so
    those are kept.
    """
    kept: list[PageEntry] = []
    removed: list[PageEntry] = []
    for entry in entries:
        if 0 < entry.word_count < min_word_count:
            removed.append(entry)
        else:
            kept.append(entry)
    return kept, FilterReport("thin content", removed)


def filter_near_duplicates(
    entries: list[PageEntry], similarity_threshold: float = 90.0
) -> tuple[list[PageEntry], FilterReport]:
    """Drop pages whose closest-similarity score meets the threshold.

    The source docstring claimed this "keeps the page with more inlinks when a
    near-duplicate is detected". It never did, and from this export it cannot: the
    Internal All report records *how similar* a page's closest match is, but not
    *which* page that is, so there is no pair to break a tie within. Run Screaming
    Frog's dedicated Near Duplicates report if you need pairwise resolution.

    What this adds over the source is a guard against culling important pages: a page
    is not dropped on similarity alone when it sits in the top decile by unique
    inlinks, because a heavily-linked near-duplicate is usually the hub and its twin
    is the thin one.
    """
    if not entries:
        return [], FilterReport("near duplicates", [])

    inlink_values = sorted((e.unique_inlinks for e in entries), reverse=True)
    protect_floor = inlink_values[max(0, len(inlink_values) // 10)]

    kept: list[PageEntry] = []
    removed: list[PageEntry] = []
    for entry in entries:
        over_threshold = entry.closest_similarity >= similarity_threshold
        protected = protect_floor > 0 and entry.unique_inlinks >= protect_floor
        if over_threshold and not protected:
            removed.append(entry)
        else:
            kept.append(entry)
    return kept, FilterReport("near duplicates", removed)
