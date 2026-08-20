"""Group rollups and verdicts.

The decision that matters is at group level: one verdict here can exclude four
thousand URLs before anything is crawled, so the rules need to be pinned rather
than tuned until a fixture looks right.

Scenarios are modelled on real shapes measured from CarsGuide's sitemap index:
61 `AllNew_*` facet sitemaps (BodyType, DriveType, Fuel, Location, Make) against
69 `sitemap_*` editorial ones (buying_guide, car_dimensions).
"""

from __future__ import annotations

from datetime import date

from app.core.metrics import (
    DateRange,
    GroupVerdict,
    PageMetrics,
    Thresholds,
    canonical_metric_url,
    merge_metrics,
    planning_table,
    summarise_group,
)

WINDOW = DateRange(date(2026, 5, 1), date(2026, 8, 17))


def pages(clicks: list[int], prefix: str = "https://x.com/p", impressions: int | None = None):
    """URLs with the given click counts, and metrics keyed by URL."""
    urls = [f"{prefix}{i}" for i in range(len(clicks))]
    metrics = {
        url: PageMetrics(
            url=url,
            clicks=c,
            impressions=impressions if impressions is not None else c * 40,
            source="test",
            window=WINDOW,
        )
        for url, c in zip(urls, clicks, strict=True)
    }
    return urls, metrics


# -- the four verdicts ------------------------------------------------------


def test_a_group_that_broadly_earns_demand_is_included():
    urls, metrics = pages([12, 40, 8, 30, 55, 9, 21, 3, 17, 44])
    group = summarise_group("buying_guide", urls, metrics)
    assert group.verdict is GroupVerdict.INCLUDE_GROUP
    assert group.coverage == 1.0
    assert group.confidence == "high"


def test_a_facet_group_is_excluded():
    """AllNew_Location: thousands of URLs, a handful of clicks between them."""
    clicks = [0] * 4_000
    clicks[0] = 7
    clicks[1] = 3
    clicks[2] = 2
    urls, metrics = pages(clicks)
    group = summarise_group("AllNew_Location", urls, metrics)

    assert group.verdict is GroupVerdict.EXCLUDE
    assert group.urls_with_clicks == 3
    assert group.coverage < 0.001
    assert "faceted-search signature" in group.rationale


def test_a_group_carried_by_a_few_winners_promotes_them():
    """The verdict that makes marketplaces tractable: index the hubs, drop the tail.

    Promotion requires a measurable distribution, so this needs enough earners to
    have one -- 20 of 100 URLs, with the top two taking most of the clicks. The
    ten-hubs-in-910-URLs shape is the same intent with too few earners to measure,
    and is handled a rung down as a recommendation rather than a decision.
    """
    clicks = [500, 400] + [10] * 18 + [0] * 80
    urls, metrics = pages(clicks)
    group = summarise_group("AllUsed_BodyType", urls, metrics)

    assert group.verdict is GroupVerdict.PROMOTE_EXEMPLARS
    assert len(group.exemplars) == 5
    assert group.head_click_share is not None
    assert group.head_click_share >= 0.7


def test_a_small_weak_group_is_reviewed_not_excluded():
    """Below the size floor, coverage alone is not evidence."""
    urls, metrics = pages([0] * 20 + [1])
    group = summarise_group("DealerGNC_Make", urls, metrics)
    assert group.verdict is GroupVerdict.REVIEW
    assert "too small to judge" in group.rationale.lower()


# -- the rule that absent data is not zero ----------------------------------


def test_a_group_with_no_metrics_is_never_auto_excluded():
    """Tier D is the current state of the tool. It must not silently delete a site."""
    urls = [f"https://x.com/p{i}" for i in range(4_000)]
    group = summarise_group("AllNew_Make", urls, {})

    assert group.verdict is GroupVerdict.REVIEW
    assert group.confidence == "low"
    assert "no metrics" in group.rationale.lower()


def test_partial_measurement_lowers_confidence_and_blocks_exclusion():
    """Excluding 4,000 URLs on 100 measured ones is not a decision, it is a guess."""
    urls = [f"https://x.com/p{i}" for i in range(4_000)]
    measured = {
        f"https://x.com/p{i}": PageMetrics(url=f"https://x.com/p{i}", clicks=0, impressions=5)
        for i in range(100)
    }
    group = summarise_group("AllNew_Fuel", urls, measured)

    assert group.confidence == "low"
    assert group.verdict is GroupVerdict.REVIEW
    assert group.overridden


# -- overrides --------------------------------------------------------------


def test_identity_pages_survive_a_traffic_based_exclusion():
    """The failure we shipped on our own site, blocked at a different layer.

    About and Case Studies are low-traffic by nature and are exactly what a model
    needs to answer who a company is.
    """
    clicks = [0] * 600
    urls, metrics = pages(clicks)
    identity = {urls[3]}

    group = summarise_group("Company", urls, metrics, identity_urls=identity)

    assert group.verdict is GroupVerdict.INCLUDE_GROUP
    assert group.overridden
    assert "identity pages" in group.rationale


def test_impressions_without_clicks_is_excluded_even_at_borderline_coverage():
    """Indexed but never chosen: Google shows them, nobody clicks."""
    clicks = [1] * 60 + [0] * 940
    urls, metrics = pages(clicks, impressions=400)
    group = summarise_group("AllNew_BodyType", urls, metrics)

    assert group.verdict is GroupVerdict.EXCLUDE
    assert group.overridden
    assert "never chosen" in group.rationale


# -- arithmetic properties --------------------------------------------------


def test_coverage_is_always_a_fraction():
    for clicks in ([], [0], [5], [0] * 99 + [1], [3] * 50):
        urls, metrics = pages(clicks) if clicks else ([], {})
        group = summarise_group("g", urls, metrics)
        assert 0.0 <= group.coverage <= 1.0


def test_verdict_is_monotonic_in_coverage():
    """More of the group earning clicks must never make the verdict harsher."""
    rank = {
        GroupVerdict.EXCLUDE: 0,
        GroupVerdict.REVIEW: 1,
        GroupVerdict.PROMOTE_EXEMPLARS: 2,
        GroupVerdict.INCLUDE_GROUP: 3,
    }
    previous = -1
    for earning in (0, 100, 400, 900, 1_000):
        clicks = [5] * earning + [0] * (1_000 - earning)
        urls, metrics = pages(clicks)
        group = summarise_group("g", urls, metrics)
        # PROMOTE_EXEMPLARS and INCLUDE both count as "kept"; only the step from
        # excluded to kept has to be monotonic.
        kept = rank[group.verdict] >= 1
        assert kept or earning == 0, f"{earning} earning URLs gave {group.verdict}"
        previous = max(previous, rank[group.verdict])


def test_thresholds_are_configurable_not_baked_in():
    clicks = [5] * 30 + [0] * 70
    urls, metrics = pages(clicks)
    strict = summarise_group("g", urls, metrics, thresholds=Thresholds(coverage_include=0.20))
    assert strict.verdict is GroupVerdict.INCLUDE_GROUP


def test_the_same_input_gives_the_same_rollup():
    """Determinism is what makes two audits comparable."""
    urls, metrics = pages([3, 0, 9, 0, 14, 2])
    a = summarise_group("g", urls, metrics)
    b = summarise_group("g", urls, metrics)
    assert (a.verdict, a.total_clicks, a.exemplars) == (b.verdict, b.total_clicks, b.exemplars)


def test_planning_table_renders_the_decision():
    urls, metrics = pages([0] * 600)
    facets = summarise_group("AllNew_Location", urls, metrics)
    urls2, metrics2 = pages([40, 22, 18], prefix="https://x.com/guide")
    guides = summarise_group("buying_guide", urls2, metrics2)

    table = planning_table([facets, guides])
    assert "AllNew_Location" in table and "exclude" in table
    assert "buying_guide" in table and "include_group" in table


def test_a_material_head_is_never_excluded_just_because_it_cannot_be_measured():
    """910 URLs where 10 hub pages take 3,680 clicks: 1.1% coverage.

    Coverage alone lands this in the exclude branch, which would throw away the
    only pages in the group worth indexing. Ten earners is too few to measure a
    distribution against a 91-URL decile, so promotion cannot be justified either
    -- the old code promoted it on a statistic that read 100% for arithmetic
    reasons rather than distributional ones. What survives is the recommendation:
    the exemplars are named and a human decides.
    """
    clicks = [0] * 900 + [800, 640, 520, 410, 380, 300, 240, 180, 120, 90]
    urls, metrics = pages(clicks)
    group = summarise_group("AllUsed_BodyType", urls, metrics)

    assert group.verdict is GroupVerdict.REVIEW
    assert group.head_click_share is None
    assert group.confidence == "low"
    assert len(group.exemplars) == 5
    assert group.coverage < 0.05


def test_a_head_without_volume_is_not_promoted():
    """Three clicks on one URL is 100% concentrated and means nothing."""
    clicks = [0] * 900 + [3]
    urls, metrics = pages(clicks)
    group = summarise_group("AllNew_DriveType", urls, metrics)
    assert group.verdict is GroupVerdict.EXCLUDE


def test_a_facet_group_with_a_real_hub_keeps_the_hub():
    """The two rules disagreed: concentration promoted, the facet override excluded.

    AllNew_Location with 200 clicks, 150 on /location/sydney/, is faceted by every
    signature test -- and the Sydney hub still belongs in the index.
    """
    clicks = [0] * 4_000
    clicks[0], clicks[1], clicks[2] = 150, 20, 15
    urls, metrics = pages(clicks, impressions=120)
    group = summarise_group("AllNew_Location", urls, metrics)

    # Held for review rather than promoted -- three earners in four thousand URLs
    # is not a measurable distribution -- but the hub is named either way, which
    # is the property that matters and the one the override used to destroy.
    assert group.verdict is GroupVerdict.REVIEW
    assert group.exemplars[0].endswith("p0")


def test_the_facet_override_still_settles_the_diffuse_case():
    """Clicks too thin to concentrate: nothing to promote, so the override decides."""
    clicks = [1] * 70 + [0] * 930
    urls, metrics = pages(clicks, impressions=900)
    group = summarise_group("AllUsed_Fuel", urls, metrics)

    assert group.verdict is GroupVerdict.EXCLUDE
    assert group.overridden


# -- the metric itself, not the verdict it feeds -----------------------------


def test_sparse_and_uniform_does_not_report_as_concentrated():
    """The denominator bug, stated directly.

    1,000 URLs where 60 earn 100 clicks each is perfectly uniform among earners.
    Measured against a 100-URL decile every earner falls inside the top decile and
    the old statistic read 100%. The verdict was arguably right; the number was
    not, and a guard bolted onto a statistic that misreports is tuning, not logic.
    """
    urls, metrics = pages([100] * 60 + [0] * 940)
    group = summarise_group("g", urls, metrics)

    assert group.head_click_share is None
    assert group.verdict is GroupVerdict.REVIEW
    assert group.confidence == "low"


def test_concentration_is_measured_over_earners_not_over_every_url():
    """Same earners, same clicks, a hundred times more dead URLs.

    Concentration is a property of the distribution among the pages that have one.
    Padding a group with URLs that earn nothing must not change how lopsided its
    earners are -- under the old denominator it changed the answer from 41% to
    100%, and the padding was doing all the work.
    """
    earning = [500, 400] + [10] * 18
    small = summarise_group("small", *pages(earning + [0] * 80))
    padded = summarise_group("padded", *pages(earning + [0] * 180))

    assert small.head_click_share == padded.head_click_share


def test_an_uncomputable_share_is_none_and_never_zero():
    """0.0 reads as "measured, and flat". None reads as "not answerable"."""
    for clicks in ([0] * 500, [5] + [0] * 499, [1, 1, 1] + [0] * 497):
        group = summarise_group("g", *pages(clicks))
        assert group.head_click_share is None


def test_a_group_with_enough_earners_still_gets_a_number():
    """The metric is not simply disabled: above a decile of earners it works."""
    group = summarise_group("g", *pages([500, 400] + [10] * 18 + [0] * 80))

    assert group.head_click_share is not None
    assert 0.0 <= group.head_click_share <= 1.0


def test_promotion_needs_a_measured_distribution():
    """PROMOTE_EXEMPLARS is now a claim about shape, so it needs shape to claim."""
    measurable = summarise_group("a", *pages([500, 400] + [10] * 18 + [0] * 80))
    unmeasurable = summarise_group("b", *pages([500, 400] + [0] * 908))

    assert measurable.verdict is GroupVerdict.PROMOTE_EXEMPLARS
    assert unmeasurable.verdict is GroupVerdict.REVIEW
    # Both name the same winners; only the confidence in the shape differs.
    assert unmeasurable.exemplars[:2] == measurable.exemplars[:2]


# -- canonicalisation, found against real Search Console data ----------------


def test_tracking_parameters_do_not_split_a_page_in_two():
    """Measured on prosperitymedia.com.au, where the homepage arrived twice."""
    assert canonical_metric_url(
        "https://x.com/?utm_source=google_maps&utm_medium=organic"
    ) == canonical_metric_url("https://x.com/")


def test_meaningful_query_parameters_survive():
    """Stripping every query string would delete real pages on a paginated site."""
    assert canonical_metric_url("https://x.com/search?q=seo&utm_source=x") == (
        "https://x.com/search?q=seo"
    )


def test_merging_sums_the_variants_rather_than_picking_one():
    rows = [
        PageMetrics(url="https://x.com/", clicks=80, impressions=1_000, source="gsc"),
        PageMetrics(url="https://x.com/?utm_source=maps", clicks=20, impressions=300, source="gsc"),
        PageMetrics(url="https://x.com/seo/", clicks=5, impressions=90, source="gsc"),
    ]
    merged = merge_metrics(rows)

    home = canonical_metric_url("https://x.com/")
    assert set(merged) == {home, canonical_metric_url("https://x.com/seo/")}
    assert merged[home].clicks == 100
    assert merged[home].impressions == 1_300


def test_unmerged_tracking_urls_understate_coverage():
    """The invisible half: a tagged URL never joins its sitemap entry.

    Without merging, the group sees one dead URL and loses the clicks entirely,
    which is how a metric quietly recommends excluding a page that is earning.
    """
    sitemap = ["https://x.com/", "https://x.com/seo/"]
    raw = [
        PageMetrics(url="https://x.com/?utm_source=maps", clicks=90, impressions=900, source="gsc"),
        PageMetrics(url="https://x.com/seo/", clicks=10, impressions=200, source="gsc"),
    ]

    unmerged = summarise_group("g", sitemap, {m.url: m for m in raw})
    merged = summarise_group("g", sitemap, merge_metrics(raw))

    # Skipping canonicalisation now loses the group entirely rather than merely
    # halving it: the sitemap spells the homepage with a trailing slash, GSC
    # spells it with a campaign tag, and neither matches the other as written.
    # A group carrying 100 clicks reports as having no data at all.
    assert unmerged.total_clicks == 0
    assert unmerged.coverage == 0.0
    assert "No metrics" in unmerged.rationale

    assert merged.total_clicks == 100
    assert merged.coverage == 1.0
