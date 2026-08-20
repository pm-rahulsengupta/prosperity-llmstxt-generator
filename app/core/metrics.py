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
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import date
from enum import StrEnum
from typing import Literal, Protocol
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlparse, urlunparse

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
    # And the winners have to be winners: a page with one click is not a hub no
    # matter how alone it is. This is also the only evidence available when the
    # concentration statistic cannot be computed at all -- see `_head_share`.
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
    # None means "not computable on this group", which is a different claim from
    # 0.0 and must not be flattened into one. See `_head_share`.
    head_click_share: float | None = None
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


# Parameters that identify a traffic source rather than a page. Search Console
# reports each variant as its own row, so a campaign-tagged homepage arrives as a
# separate page from the homepage.
TRACKING_PARAMS = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "utm_id",
        "gclid",
        "gbraid",
        "wbraid",
        "fbclid",
        "msclkid",
        "dclid",
        "mc_cid",
        "mc_eid",
        "_ga",
        "ref",
        "referrer",
    }
)


def _normalise_path(path: str) -> str:
    """One spelling per path.

    Percent-encoding is decoded and re-encoded so `/caf%C3%A9/` and `/café/`
    become one key -- `unquote` first because a path may arrive either way, and
    `quote` after so the result is a legal URL rather than raw bytes. `safe`
    keeps the separators that carry meaning.

    The trailing slash is dropped rather than added, because the root is the one
    path that cannot lose it and a bare "" is not a path.
    """
    decoded = unquote(path or "/")
    normalised = quote(decoded.lower(), safe="/~:@!$&'()*+,;=")
    if len(normalised) > 1 and normalised.endswith("/"):
        normalised = normalised.rstrip("/") or "/"
    return normalised or "/"


@dataclass(frozen=True, slots=True)
class CanonicalPolicy:
    """Per-site canonicalisation rules.

    `meaningful_params` is the part that cannot be global. A blanket strip is as
    wrong as no strip: `?page=2` and `?sort=price` are distinct pages on a
    marketplace and noise everywhere else, so the allowlist is declared per site
    rather than guessed. Default behaviour strips known tracking keys only.
    """

    meaningful_params: frozenset[str] = frozenset()
    # The host the property reports under. GSC keys a URL-prefix property to one
    # host, so `www.x.com` and `x.com` arrive as different pages when a site
    # answers on both; folding to the property's own form is what rejoins them.
    canonical_host: str = ""


DEFAULT_POLICY = CanonicalPolicy()


def canonical_metric_url(url: str, policy: CanonicalPolicy | None = None) -> str:
    """Strip tracking parameters and normalise the trailing slash.

    Measured on prosperitymedia.com.au: the homepage arrives from GSC as both
    `/` and `/?utm_source=google_maps&utm_medium=organic&utm_campaign=local`, and
    both showed up as separate exemplars. Splitting one page's clicks in two is
    the visible half of the problem. The invisible half is worse -- a tagged URL
    never matches the sitemap entry it belongs to, so its clicks do not merely
    double-count, they are dropped from the group and coverage reads lower than
    the truth. A metric that silently understates demand would exclude pages that
    are earning.

    UTM was one of at least six ways the same join fails, and every one of them
    fails quietly, so each is handled here explicitly:

    * **scheme** -- `http` and `https` are the same page; fold to `https`.
    * **host** -- `www.x.com` and `x.com` are the same page when the site answers
      on both; fold to the property's host when the policy names one.
    * **path case** -- servers commonly serve `/SEO-Melbourne/` and
      `/seo-melbourne/` identically. The *query* is left alone: `?q=Sydney` and
      `?q=sydney` are not reliably the same search.
    * **trailing slash** -- GSC reports both forms.
    * **percent-encoding** -- `/caf%C3%A9/` and `/café/` are one page.
    * **fragment** -- never sent to a server; it cannot identify a page.

    Query parameters are sorted and de-duplicated so that ordering, which no
    client guarantees, cannot produce two keys for one page.

    Only known tracking keys are removed. Stripping every query string would
    destroy real pages on any site that paginates or filters through one, which
    is why `policy.meaningful_params` exists rather than a global rule.
    """
    policy = policy or DEFAULT_POLICY
    parsed = urlparse(url)

    scheme = "https" if parsed.scheme in ("http", "https", "") else parsed.scheme
    host = parsed.netloc.lower()
    if host.endswith(":443"):
        host = host[:-4]
    if host.endswith(":80"):
        host = host[:-3]
    if policy.canonical_host:
        # Fold both directions to whatever the property reports under, rather
        # than always stripping `www` -- plenty of properties *are* the www host.
        wanted = policy.canonical_host.lower()
        if host in (wanted, f"www.{wanted}") or f"www.{host}" == wanted:
            host = wanted
    elif host.startswith("www."):
        host = host[4:]

    kept = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in TRACKING_PARAMS or key.lower() in policy.meaningful_params
    ]
    # Sorted and de-duplicated: parameter order is not guaranteed by anything
    # upstream, and `?a=1&a=1` is the same request as `?a=1`.
    kept = sorted(set(kept))

    path = _normalise_path(parsed.path)
    return urlunparse((scheme, host, path, "", urlencode(kept), ""))


def merge_metrics(rows: Iterable[PageMetrics]) -> dict[str, PageMetrics]:
    """Key metrics by canonical URL, summing the variants that collapse together.

    Clicks and impressions add; the date window is carried from the first row,
    since every row in one fetch shares it.
    """
    merged: dict[str, PageMetrics] = {}
    for row in rows:
        key = canonical_metric_url(row.url)
        if (existing := merged.get(key)) is None:
            merged[key] = replace(row, url=key)
        else:
            merged[key] = replace(
                existing,
                clicks=(existing.clicks or 0) + (row.clicks or 0),
                impressions=(existing.impressions or 0) + (row.impressions or 0),
            )
    return merged


@dataclass(frozen=True, slots=True)
class JoinReport:
    """How well the metrics joined the site's own URLs.

    More canonicalisation rules cannot protect against the next unknown way the
    join fails -- there were six known ones and the sixth was found by accident.
    A visible number can. Three percent of rows failing to join is ordinary
    noise: redirects, pages retired since the window, hosts we do not own.
    Fourteen percent means something is broken, and the point of surfacing it is
    that someone sees it the same day rather than after a client asks why a page
    is missing from their file.
    """

    total_rows: int = 0
    joined_rows: int = 0
    orphan_rows: int = 0
    orphan_clicks: int = 0
    total_clicks: int = 0
    # A capped sample, so the cause is diagnosable without re-running a fetch
    # that costs quota and may not reproduce the same window.
    orphan_sample: tuple[str, ...] = ()

    @property
    def orphan_share(self) -> float:
        return self.orphan_rows / self.total_rows if self.total_rows else 0.0

    @property
    def orphan_click_share(self) -> float:
        """Weighted by clicks, which is the half that matters.

        Ten thousand orphaned rows earning nothing is tidy-up. Ten orphaned rows
        holding a fifth of the site's traffic is a broken join wearing a small
        row count as a disguise.
        """
        return self.orphan_clicks / self.total_clicks if self.total_clicks else 0.0

    @property
    def looks_broken(self) -> bool:
        return self.orphan_share > 0.10 or self.orphan_click_share > 0.10

    def summary(self) -> str:
        if not self.total_rows:
            return "No metric rows to join."
        verdict = " — check the join" if self.looks_broken else ""
        return (
            f"{self.joined_rows:,} of {self.total_rows:,} metric rows matched a known URL; "
            f"{self.orphan_rows:,} did not ({self.orphan_share:.1%} of rows, "
            f"{self.orphan_click_share:.1%} of clicks){verdict}."
        )


def join_metrics(
    known_urls: Iterable[str],
    metrics: dict[str, PageMetrics],
    sample_size: int = 20,
) -> JoinReport:
    """Report how many metric rows failed to match any URL the site declares.

    Deliberately separate from `merge_metrics`: merging is about collapsing
    spellings of one page, joining is about whether the result lines up with the
    site at all. Conflating them would hide the second question inside a function
    whose success is measured by the first.
    """
    canonical_known = {canonical_metric_url(url) for url in known_urls}
    orphans = [m for url, m in sorted(metrics.items()) if url not in canonical_known]

    return JoinReport(
        total_rows=len(metrics),
        joined_rows=len(metrics) - len(orphans),
        orphan_rows=len(orphans),
        orphan_clicks=sum(m.clicks or 0 for m in orphans),
        total_clicks=sum(m.clicks or 0 for m in metrics.values()),
        # Sampled by clicks, not arbitrarily: the orphans worth diagnosing are
        # the ones carrying traffic.
        orphan_sample=tuple(
            m.url for m in sorted(orphans, key=lambda m: -(m.clicks or 0))[:sample_size]
        ),
    )


def _head_share(clicks: list[int]) -> float | None:
    """How lopsided the earning URLs are, or None when that cannot be answered.

    Concentration is a claim about a distribution, so it has to be measured over
    the URLs that have one. Measuring the top decile of *all* URLs instead makes
    the denominator do the work: if fewer URLs earn clicks than the decile is
    wide, every earner falls inside the top decile and the statistic reads 100%
    however evenly those clicks are spread. A thousand URLs where sixty earn a
    hundred clicks each is perfectly uniform and used to report as perfectly
    concentrated.

    So there are two questions here, and the old function conflated them:

    * Is the distribution lopsided? Answerable only when there are enough earners
      to have a shape -- at least a decile's worth. Then it is the share of clicks
      taken by the top tenth *of earners*.
    * Are there enough earners to ask? When there are not, the honest answer is
      None. Not 0.0, which reads as "measured, and flat", and not 1.0, which reads
      as "measured, and extreme". The caller must then decide on other evidence,
      and `summarise_group` uses the absolute size of the head instead.
    """
    earners = sorted((c for c in clicks if c > 0), reverse=True)
    total = sum(earners)
    if not total:
        return None
    # The decile width of the group as a whole is what makes the old statistic
    # degenerate, so it is what decides whether the question is answerable.
    if len(earners) < max(1, len(clicks) // 10):
        return None
    cut = max(1, len(earners) // 10)
    return sum(earners[:cut]) / total


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

    # Both sides of the join are canonicalised. Metrics arrive already keyed that
    # way from `merge_metrics`; the group's URLs come from a sitemap and have not
    # been touched, so looking them up raw would miss on nothing more exotic than
    # a trailing slash -- and miss *silently*, reporting a group with real traffic
    # as having none. That is the same failure the canonicalisation exists to
    # prevent, one layer further in.
    lookup = [(url, canonical_metric_url(url)) for url in urls]
    known = [metrics[key] for _, key in lookup if key in metrics and metrics[key].has_search_data]
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
    group.head_click_share = _head_share(clicks)
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
    # Volume is a precondition for promotion under either path: a group has to
    # have earned something before the shape of its earnings matters.
    material = (
        group.total_clicks >= thresholds.promote_min_clicks
        and best >= thresholds.promote_min_exemplar_clicks
    )
    concentrated = (
        group.head_click_share is not None
        and group.head_click_share >= thresholds.concentration
        and material
    )
    # Too few earners to measure a distribution, but the head is substantial. The
    # old code promoted these on a statistic that read 100% for arithmetic
    # reasons; excluding them instead would be the opposite error, since this is
    # the marketplace shape the promote verdict exists for -- ten hub pages
    # carrying nine hundred dead ones. With no distributional evidence the honest
    # move is neither: recommend the exemplars and let a person confirm them.
    unmeasurable_but_material = group.head_click_share is None and material

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
            f"Only {coverage:.1%} of URLs earn clicks, but the top tenth of those take "
            f"{group.head_click_share:.0%} of {group.total_clicks:,} clicks — index "
            "the winners, drop the tail."
        )
    elif unmeasurable_but_material:
        group.verdict = GroupVerdict.REVIEW
        group.confidence = "low"
        group.rationale = (
            f"{group.urls_with_clicks} of {group.url_count:,} URLs earn "
            f"{group.total_clicks:,} clicks, the best of them {best:,} — too few earners "
            "to tell a hub from an even spread, so this is a recommendation and not a "
            "decision. The exemplars are the pages worth keeping if you agree."
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
    # It must not fire on a group that has exemplars. A facet group can hold a
    # genuine hub -- `/location/sydney/` taking 150 of a group's 200 clicks -- and
    # forcing the whole group out discards the one page in it worth indexing.
    #
    # The condition is exemplars rather than verdict on purpose. It was written as
    # `verdict is not PROMOTE_EXEMPLARS` first, which held only while promotion was
    # the sole verdict that identified winners; the moment an unmeasurable-but-
    # material group started arriving here as REVIEW-with-exemplars, the override
    # ate it and the Sydney hub disappeared again. What this rule actually wants to
    # settle is the diffuse case -- impressions at scale with nothing worth keeping
    # -- and a group with exemplars is by definition not that.
    if (
        not group.exemplars
        and group.verdict is not GroupVerdict.EXCLUDE
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
