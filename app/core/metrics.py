"""Search and analytics metrics, and the group-level decisions they support.

Pure: no I/O, no vendor imports, no credentials. Adapters live in `app/metrics/`
and hand `PageMetrics` in; everything here is arithmetic and stated rules.

Two problems this exists to solve, neither of which is fixed by adding a weight.

**Curation is happening by truncation.** `select_urls` ends `ordered[:page_cap]`.
At cap 400 against 223 URLs it never bound; at cap 60 it did all the work. Metrics
move the decision up to the *sitemap group*, so most URLs are excluded by a reasoned
call before anything is crawled and the cap stops being the curator.

**Internal-graph ranking over-values faceted search by construction.** Link Score and
Unique Inlinks are a good prior — they are the site's own statement of what matters,
available with no client credentials, and they do not collapse on pages that have
never ranked. But faceted navigation is heavily internally linked *by design*.
CarsGuide's `AllNew_Location` will look important to every internal metric and be
worthless in an index. Search demand is the only available corrective.

So metrics adjust the internal prior; they never replace it.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Literal, Protocol

from app.core.onboarding import SiteBrief, matches_any

Confidence = Literal["high", "medium", "low"]

Tier = Literal["A", "B", "C", "D"]


@dataclass(frozen=True, slots=True)
class DateRange:
    start: date
    end: date

    @property
    def days(self) -> int:
        return (self.end - self.start).days

    def __str__(self) -> str:
        return f"{self.start.isoformat()}..{self.end.isoformat()}"


@dataclass(frozen=True, slots=True)
class PageMetrics:
    """What one adapter knows about one URL.

    Every field is optional and `None` means *unknown*, never zero. The difference
    matters at every decision point below: a page with no clicks and a page we have
    no click data for lead to opposite verdicts.
    """

    url: str
    clicks: int | None = None
    impressions: int | None = None
    ctr: float | None = None
    position: float | None = None
    query_count: int | None = None
    top_query: str | None = None
    conversions: float | None = None
    engagement_seconds: float | None = None
    referring_domains: int | None = None
    ai_citations: int | None = None
    source: str = ""
    window: DateRange | None = None

    @property
    def has_search_data(self) -> bool:
        return self.clicks is not None or self.impressions is not None


class GroupVerdict(StrEnum):
    INCLUDE_GROUP = "include_group"
    PROMOTE_EXEMPLARS = "promote_exemplars"
    EXCLUDE = "exclude"
    REVIEW = "review"


@dataclass(frozen=True, slots=True)
class Thresholds:
    """Every number the verdict depends on, in one place and overridable."""

    coverage_include: float = 0.40
    coverage_exclude: float = 0.05
    concentration: float = 0.70
    min_group_size_for_exclude: int = 500
    # Concentration alone is not evidence: three clicks landing on one URL is 100%
    # concentrated and means nothing. A group must earn this many clicks in total
    # before its winners are worth promoting.
    promote_min_clicks: int = 50
    # And the winners have to be winners. `top_decile_click_share` reports 100%
    # whenever fewer URLs earn clicks than the decile is wide -- 60 URLs with one
    # click each in a group of 1,000 looks perfectly concentrated and is perfectly
    # uniform. What separates a hub from a facet is not the shape of the
    # distribution but the absolute size of its head: a page with one click is not
    # a hub no matter how alone it is.
    promote_min_exemplar_clicks: int = 10
    # An impressions-heavy, click-poor group is the faceted-search signature even
    # when coverage is borderline.
    facet_min_impressions: int = 10_000
    facet_max_ctr: float = 0.002


@dataclass(slots=True)
class GroupMetrics:
    """A sitemap group, rolled up, with a verdict and the reason for it."""

    group_key: str
    url_count: int = 0
    urls_with_clicks: int = 0
    total_clicks: int = 0
    total_impressions: int = 0
    median_clicks: float = 0.0
    p90_clicks: float = 0.0
    top_decile_click_share: float = 0.0
    exemplars: list[str] = field(default_factory=list)
    verdict: GroupVerdict = GroupVerdict.REVIEW
    confidence: Confidence = "low"
    # Why this verdict, in one line, for the human at the review gate.
    rationale: str = ""
    # True when a rule overrode what the numbers alone would have said.
    overridden: bool = False
    # The onboarding answer that moved this verdict, verbatim, so the planning
    # table can show which declaration is responsible for which group.
    declared: str = ""

    @property
    def coverage(self) -> float:
        """Share of the group's URLs that earn any clicks at all.

        The headline number, not `total_clicks`. Four thousand URLs with twelve
        clicks between them and twelve URLs with twelve clicks are opposite
        decisions, and only coverage separates them.
        """
        return self.urls_with_clicks / self.url_count if self.url_count else 0.0

    @property
    def mean_ctr(self) -> float:
        return self.total_clicks / self.total_impressions if self.total_impressions else 0.0


def _top_decile_share(clicks: list[int]) -> float:
    """What fraction of the group's clicks the best 10% of its URLs account for.

    High concentration with low coverage means a few real pages are carrying a mass
    of dead ones -- index those and drop the tail, rather than taking or rejecting
    the group whole.
    """
    total = sum(clicks)
    if not total:
        return 0.0
    ranked = sorted(clicks, reverse=True)
    cut = max(1, len(ranked) // 10)
    return sum(ranked[:cut]) / total


def summarise_group(
    group_key: str,
    urls: list[str],
    metrics: dict[str, PageMetrics],
    *,
    thresholds: Thresholds | None = None,
    identity_urls: set[str] | None = None,
    brief: SiteBrief | None = None,
    exemplar_limit: int = 5,
) -> GroupMetrics:
    """Roll a sitemap group up and decide what to do with it.

    Deterministic. The verdict is always overridable by the human at stage 1, and
    `rationale` exists so that override is an informed one.
    """
    thresholds = thresholds or Thresholds()
    group = GroupMetrics(group_key=group_key, url_count=len(urls))

    known = [metrics[url] for url in urls if url in metrics and metrics[url].has_search_data]
    if not known:
        group.verdict = GroupVerdict.REVIEW
        group.confidence = "low"
        group.rationale = "No metrics for any URL in this group; nothing to judge on."
        return group

    clicks = [m.clicks or 0 for m in known]
    group.total_clicks = sum(clicks)
    group.total_impressions = sum(m.impressions or 0 for m in known)
    group.urls_with_clicks = sum(1 for c in clicks if c > 0)
    group.median_clicks = statistics.median(clicks) if clicks else 0.0
    group.p90_clicks = (
        statistics.quantiles(clicks, n=10)[-1]
        if len(clicks) >= 10
        else float(max(clicks, default=0))
    )
    group.top_decile_click_share = _top_decile_share(clicks)
    group.exemplars = [
        m.url
        for m in sorted(known, key=lambda m: -(m.clicks or 0))[:exemplar_limit]
        if (m.clicks or 0) >= thresholds.promote_min_exemplar_clicks
    ]

    # Confidence is about how much of the group we actually measured, not about how
    # good the numbers are.
    measured = len(known) / group.url_count if group.url_count else 0
    group.confidence = "high" if measured >= 0.8 else "medium" if measured >= 0.4 else "low"

    coverage = group.coverage
    best = max((m.clicks or 0) for m in known)
    concentrated = (
        group.top_decile_click_share >= thresholds.concentration
        and group.total_clicks >= thresholds.promote_min_clicks
        and best >= thresholds.promote_min_exemplar_clicks
    )

    if coverage >= thresholds.coverage_include:
        group.verdict = GroupVerdict.INCLUDE_GROUP
        group.rationale = (
            f"{coverage:.0%} of {group.url_count} URLs earn clicks — the group broadly "
            "earns search demand."
        )
    elif concentrated:
        # Checked *before* the low-coverage exclusion, not inside the middle band.
        # A marketplace facet group is 910 URLs where 10 hub pages take 3,680 clicks:
        # coverage is 1%, which lands in the exclude branch, and excluding it throws
        # away the only pages in the group worth indexing. Concentration is the
        # signal that a group has real winners regardless of how dead its tail is,
        # and it is exactly what this verdict exists for.
        group.verdict = GroupVerdict.PROMOTE_EXEMPLARS
        group.rationale = (
            f"Only {coverage:.1%} of URLs earn clicks, but the top decile takes "
            f"{group.top_decile_click_share:.0%} of {group.total_clicks:,} clicks — index "
            "the winners, drop the tail."
        )
    elif coverage >= thresholds.coverage_exclude:
        group.verdict = GroupVerdict.REVIEW
        group.rationale = (
            f"{coverage:.0%} coverage and no concentration — diffuse and mediocre, "
            "needs a human call."
        )
    elif group.url_count > thresholds.min_group_size_for_exclude:
        group.verdict = GroupVerdict.EXCLUDE
        group.rationale = (
            f"{group.url_count:,} URLs and only {group.urls_with_clicks} earn any clicks "
            f"({coverage:.1%}) — the faceted-search signature."
        )
    else:
        group.verdict = GroupVerdict.REVIEW
        group.rationale = (
            f"Only {group.url_count} URLs and {coverage:.0%} coverage — too small to judge "
            "on coverage alone."
        )

    return _apply_overrides(group, urls, known, thresholds, identity_urls or set(), brief)


def _apply_overrides(
    group: GroupMetrics,
    urls: list[str],
    known: list[PageMetrics],
    thresholds: Thresholds,
    identity_urls: set[str],
    brief: SiteBrief | None = None,
) -> GroupMetrics:
    """Rules that beat the arithmetic, applied after it."""

    # Embargo is not evidence to be weighed against clicks. It comes first and
    # nothing below can undo it.
    if brief and brief.embargoed:
        embargo = matches_any(group.group_key, brief.embargoed) or next(
            (m for url in urls if (m := matches_any(url, brief.embargoed))), None
        )
        if embargo:
            group.verdict = GroupVerdict.EXCLUDE
            group.overridden = True
            group.declared = embargo
            group.rationale = f"Excluded under embargo: matches {embargo!r}."
            return group  # Absolute: the one verdict the brief itself cannot revisit.

    if brief and brief.must_appear:
        identity_urls = identity_urls | {
            url for url in urls if matches_any(url, tuple(brief.must_appear))
        }

    # Identity pages are low-traffic by nature and are exactly what a model needs to
    # answer "who is this company and how do I contact them". We shipped a file for
    # our own site with Case Studies and About in `## Optional`; this is the guard
    # against doing it again by a different route.
    if identity_urls and any(url in identity_urls for url in urls):
        if group.verdict is GroupVerdict.EXCLUDE:
            group.verdict = GroupVerdict.INCLUDE_GROUP
            group.overridden = True
            group.rationale = (
                "Contains identity pages (about / contact / case studies), which are "
                "low-traffic by nature and cannot be excluded on traffic."
            )
        return _apply_brief(group, urls, brief)

    # Impressions without clicks, at scale, is faceted search even when coverage is
    # borderline: Google indexes the pages and nobody picks them.
    #
    # It must not fire on PROMOTE_EXEMPLARS. A facet group can hold a genuine hub --
    # `/location/sydney/` taking 150 of a group's 200 clicks -- and forcing the whole
    # group out discards the one page in it worth indexing. Where exemplars exist,
    # promoting them already demotes the tail, which is what this override wants;
    # the two rules would otherwise disagree about the same group. The override still
    # earns its place on the diffuse case: a facet group whose clicks are spread too
    # thinly to concentrate lands in REVIEW, and this is what settles it.
    if (
        group.verdict not in (GroupVerdict.EXCLUDE, GroupVerdict.PROMOTE_EXEMPLARS)
        and group.total_impressions >= thresholds.facet_min_impressions
        and group.mean_ctr < thresholds.facet_max_ctr
        and group.url_count > thresholds.min_group_size_for_exclude
    ):
        group.verdict = GroupVerdict.EXCLUDE
        group.overridden = True
        group.rationale = (
            f"{group.total_impressions:,} impressions at {group.mean_ctr:.2%} CTR across "
            f"{group.url_count:,} URLs — indexed but never chosen."
        )
        return _apply_brief(group, urls, brief)

    # Never exclude on the absence of history. A page published last week has had no
    # opportunity to rank, and excluding it is how a site's newest work disappears.
    if group.verdict is GroupVerdict.EXCLUDE and group.confidence == "low":
        group.verdict = GroupVerdict.REVIEW
        group.overridden = True
        group.rationale += " Too little of the group was measured to exclude it outright."

    return _apply_brief(group, urls, brief)


def _apply_brief(
    group: GroupMetrics,
    urls: list[str],
    brief: SiteBrief | None,
) -> GroupMetrics:
    """What the operator declared, applied last, as a floor and a ceiling.

    Last because it should move a verdict the evidence actually reached, not
    pre-empt the arithmetic. And bounded in both directions: a declaration
    changes how far a verdict may travel, never where it lands. An operator who
    declares a pattern valuable stops it being deleted on traffic alone and buys
    nothing else; one who declares a pattern noise stops it being swallowed
    wholesale without deleting the page in it that earns real clicks.
    """
    if brief is None:
        return group

    def declared(patterns: tuple[str, ...]) -> str | None:
        return matches_any(group.group_key, patterns) or next(
            (m for url in urls if (m := matches_any(url, patterns))), None
        )

    # The floor. Exclusion is the only verdict it blocks, because it is the only
    # one that removes pages without a human seeing them.
    if group.verdict is GroupVerdict.EXCLUDE and (pattern := declared(brief.valuable)):
        group.overridden = True
        group.declared = pattern
        if group.exemplars:
            group.verdict = GroupVerdict.PROMOTE_EXEMPLARS
            group.rationale = (
                f"You declared {pattern!r} valuable, so it is not excluded on traffic. "
                f"{len(group.exemplars)} page(s) here earn clicks and carry the group."
            )
        else:
            group.verdict = GroupVerdict.REVIEW
            group.rationale = (
                f"You declared {pattern!r} valuable, so it is not excluded on traffic — but "
                f"{group.url_count:,} URLs earn {group.total_clicks} clicks between them. "
                "Held for you to look at."
            )
        return group

    # The ceiling, its mirror. It stops wholesale inclusion without deleting a
    # page that has demonstrably earned its place.
    if group.verdict is GroupVerdict.INCLUDE_GROUP and (pattern := declared(brief.noise)):
        group.overridden = True
        group.declared = pattern
        if group.exemplars:
            group.verdict = GroupVerdict.PROMOTE_EXEMPLARS
            group.rationale = (
                f"You declared {pattern!r} low value, so the group is not included wholesale. "
                f"{len(group.exemplars)} page(s) in it earn real clicks and are kept."
            )
        else:
            group.verdict = GroupVerdict.REVIEW
            group.rationale = f"You declared {pattern!r} low value. Held rather than included."

    return group


def planning_table(groups: list[GroupMetrics]) -> str:
    """The group rollup as the human sees it at the review gate.

    This is the artefact that changes the shape of the tool: one line here can
    exclude four thousand URLs before a single page is fetched.
    """
    if not groups:
        return "No sitemap groups."

    ordered = sorted(groups, key=lambda g: (-g.total_clicks, -g.url_count))
    width = max(len(g.group_key) for g in ordered)
    lines = [
        f"{'GROUP'.ljust(width)}  {'URLS':>7} {'CLICKS':>8} {'COVER':>6}  VERDICT",
    ]
    for g in ordered:
        lines.append(
            f"{g.group_key.ljust(width)}  {g.url_count:>7,} {g.total_clicks:>8,} "
            f"{g.coverage:>5.0%}  {g.verdict.value}" + ("  (overridden)" if g.overridden else "")
        )
    return "\n".join(lines)


class MetricsProvider(Protocol):
    """What an adapter must offer. Implementations live in `app/metrics/`."""

    def tier(self) -> Tier: ...

    def fetch(self, domain: str, urls: list[str], window: DateRange) -> dict[str, PageMetrics]: ...
