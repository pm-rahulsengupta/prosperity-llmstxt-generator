"""The stage-1 planning table, keyed on sitemap group rather than path template.

Path templates were the wrong axis and CarsGuide is the proof. Its 11,909 URLs
cluster into 397 templates that are placeholder soup -- `/{slug}/{slug}/{slug}`
repeated in variations -- while its 167 sitemap names are a clean taxonomy:
`AllNew_BodyType`, `AllUsed_Make`, `sitemap_ev9d9_buying_guide`. The site is
telling us how it is organised, in its own words, and the planner was reading
URL shapes instead.

Provenance is primary at both extremes. On a flat WordPress site every URL
collapses to one `/{slug}` template and path shape carries nothing at all, while
`post-sitemap`, `page-sitemap` and `seo_services-sitemap` separate cleanly. On a
marketplace the templates are noise and the sitemap names are a taxonomy. The
axis that works on both is the one the site publishes.

Everything here works at tier D with no metrics. That is the normal state of the
tool: `url_count`, `template_diversity`, `sample_urls` and multi-membership all
come from a sitemap fetch and cost nothing, and the table renders in full before
a single click is known. Metrics, when present, confirm or overturn -- they never
have to be there for the planner to see structure.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Literal

from app.core.metrics import GroupMetrics, GroupVerdict, PageMetrics, summarise_group
from app.core.onboarding import SiteBrief
from app.scrape.recon import SiteRecon, cluster_urls

__all__ = [
    "UNSOURCED",
    "GroupIntent",
    "GroupRow",
    "build_planning_table",
    "render_planning_table",
]

# URLs discovered without a sitemap to attribute them to. Never auto-excluded:
# not knowing where a page came from is not evidence about the page.
UNSOURCED = "__unsourced__"

GroupIntent = Literal["editorial", "faceted", "hub", "utility", "unknown"]

# How many URLs to show the model and the operator per group. Enough to see the
# shape, few enough that 167 groups still fit in one planning call.
SAMPLE_URLS = 6


@dataclass(slots=True)
class GroupRow:
    """One sitemap group, everything known about it, and what to do with it."""

    group_key: str
    url_count: int = 0
    # Distinct path templates within the group. The strongest tier-D signal
    # available and free: a group whose URLs all share one template is
    # machine-generated, while several templates suggests something editorial.
    # It separates `AllNew_Location` from `sitemap_buying_guide` before a single
    # click is known.
    template_diversity: int = 0
    sample_urls: list[str] = field(default_factory=list)
    # How many URLs in this group are listed by more than one sitemap. A page
    # several sitemaps point at is usually a hub.
    multi_listed: int = 0
    intent: GroupIntent = "unknown"
    intent_reason: str = ""
    metrics: GroupMetrics | None = None

    @property
    def verdict(self) -> GroupVerdict:
        return self.metrics.verdict if self.metrics else GroupVerdict.REVIEW

    @property
    def confidence(self) -> str:
        return self.metrics.confidence if self.metrics else "low"

    @property
    def exemplars(self) -> list[str]:
        return self.metrics.exemplars if self.metrics else []

    @property
    def declared(self) -> str:
        """The brief pattern that moved this verdict, if one did."""
        return self.metrics.declared if self.metrics else ""

    @property
    def rationale(self) -> str:
        if self.metrics and self.metrics.rationale:
            return self.metrics.rationale
        return "No metrics for this site yet; nothing is excluded without evidence."

    @property
    def uniform(self) -> bool:
        """One template across the whole group: machine-generated, almost surely."""
        return self.url_count > 1 and self.template_diversity == 1


def build_planning_table(
    recon: SiteRecon,
    metrics: dict[str, PageMetrics] | None = None,
    brief: SiteBrief | None = None,
    identity_urls: set[str] | None = None,
    intents: dict[str, tuple[GroupIntent, str]] | None = None,
    sample_urls: int = SAMPLE_URLS,
) -> list[GroupRow]:
    """Roll a site's sitemaps into the table stage 1 plans against.

    Ordered by URL count, because that is the order in which decisions matter:
    the group holding four thousand URLs is the one worth a person's attention,
    whatever its name sorts as.
    """
    metrics = metrics or {}
    intents = intents or {}

    grouped: dict[str, list[str]] = {}
    for url in recon.urls:
        key = recon.url_sources.get(url)
        grouped.setdefault(_label(key), []).append(url)

    rows: list[GroupRow] = []
    for key, urls in grouped.items():
        intent, reason = intents.get(key, ("unknown", ""))
        row = GroupRow(
            group_key=key,
            url_count=len(urls),
            template_diversity=len(cluster_urls(urls)),
            sample_urls=_sample(urls, sample_urls),
            multi_listed=sum(1 for url in urls if recon.multi_listed(url) > 1),
            intent=intent,
            intent_reason=reason,
        )

        if key == UNSOURCED:
            # Held always. A URL we could not attribute is a gap in our knowledge
            # of the site, not a fact about the page, and excluding on it would
            # turn a discovery failure into a deletion.
            row.metrics = None
        else:
            row.metrics = summarise_group(
                key, urls, metrics, identity_urls=identity_urls, brief=brief
            )
        rows.append(row)

    rows.sort(key=lambda r: (-r.url_count, r.group_key))
    return rows


def _label(sitemap_url: str | None) -> str:
    return sitemap_url or UNSOURCED


def _sample(urls: list[str], size: int) -> list[str]:
    """A spread through the group, not the first few.

    Sitemaps are commonly ordered by date or by id, so the first N URLs are the
    least representative slice available -- all from one week, or all from one
    section. Sampling at an even stride costs nothing and shows the range.
    """
    if len(urls) <= size:
        return list(urls)
    stride = len(urls) / size
    return [urls[int(i * stride)] for i in range(size)]


def render_planning_table(rows: list[GroupRow]) -> str:
    """The table a person reads at the review gate.

    One line here can exclude four thousand URLs before anything is fetched, so
    it shows the evidence next to the verdict rather than the verdict alone.
    """
    if not rows:
        return "No sitemap groups found."

    width = min(42, max(len(r.group_key) for r in rows))
    header = (
        f"{'GROUP'.ljust(width)}  {'URLS':>7} {'TMPL':>5} {'MULTI':>6} "
        f"{'INTENT':<10} {'VERDICT':<18} CONF"
    )
    lines = [header, "-" * len(header)]

    for row in rows:
        name = row.group_key if len(row.group_key) <= width else row.group_key[: width - 1] + "…"
        lines.append(
            f"{name.ljust(width)}  {row.url_count:>7,} {row.template_diversity:>5} "
            f"{row.multi_listed:>6} {row.intent:<10} {row.verdict.value:<18} {row.confidence}"
        )
        if row.declared:
            lines.append(f"{' ' * width}    declared: {row.declared}")
        if row.exemplars:
            lines.append(f"{' ' * width}    keep: {', '.join(row.exemplars[:3])}")

    counts = Counter(row.verdict.value for row in rows)
    lines.append("")
    lines.append(
        f"{len(rows)} group(s), {sum(r.url_count for r in rows):,} URLs. "
        + ", ".join(f"{n} {verdict}" for verdict, n in sorted(counts.items()))
    )
    return "\n".join(lines)
