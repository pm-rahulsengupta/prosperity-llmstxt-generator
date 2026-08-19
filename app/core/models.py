"""Typed records shared by the whole pipeline.

The source tool passed bare `Dict` everywhere, and hand-picked subsets of those
dicts between stages. That is how it lost `link_score` and `word_count` on the
CSV+AI path and every ranking signal on the crawl path — the 40%-weighted term
silently became zero and nobody could see it. A dataclass makes the loss
impossible: stages carry the whole record or they do not compile.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any


@dataclass(slots=True)
class PageEntry:
    """One candidate page, from any discovery source.

    Screaming Frog fills the link-graph fields directly. The crawler derives
    `crawl_depth` from its own traversal and leaves `link_score` at 0 — it has no
    equivalent, which is exactly why CSV import is worth keeping.
    """

    url: str
    title: str = ""
    description: str = ""
    h1: str = ""

    # Content signals
    word_count: int = 0
    text_ratio: float = 0.0

    # Link-graph signals. `crawl_depth` defaults to -1, not 0, so "unknown" is
    # distinguishable from "homepage". The source defaulted it to 0, which made
    # every crawled page look like the root and left `## Optional` permanently empty.
    crawl_depth: int = -1
    folder_depth: int = 0
    inlinks: int = 0
    unique_inlinks: int = 0
    outlinks: int = 0
    external_outlinks: int = 0
    link_score: int = 0

    # Dedup signals
    content_hash: str = ""
    canonical: str = ""
    closest_similarity: float = 0.0

    # Fetch results
    markdown: str = ""
    status_code: int = 0
    fetch_tier: str = ""
    response_time: float = 0.0

    # Assigned downstream
    index: int = 0
    section: str = ""
    is_optional: bool = False

    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def display_title(self) -> str:
        return self.title or self.h1 or ""

    def with_(self, **changes: Any) -> PageEntry:
        return replace(self, **changes)


@dataclass(slots=True)
class Section:
    name: str
    description: str = ""
    pages: list[PageEntry] = field(default_factory=list)
    position: int = 0


@dataclass(slots=True)
class ValidationIssue:
    level: str  # "error" | "warning" | "info"
    message: str
    code: str = ""


@dataclass(slots=True)
class GenerationResult:
    site_url: str
    site_name: str
    site_summary: str
    pattern: str
    sections: list[Section] = field(default_factory=list)
    optional: list[PageEntry] = field(default_factory=list)
    llmstxt: str = ""
    llms_full: str = ""
    issues: list[ValidationIssue] = field(default_factory=list)
    pages_total: int = 0

    @property
    def pages_included(self) -> int:
        return sum(len(s.pages) for s in self.sections) + len(self.optional)
