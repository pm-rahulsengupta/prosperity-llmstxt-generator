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
    """The verdict that makes marketplaces tractable: index the hubs, drop the tail."""
    clicks = [0] * 900 + [800, 640, 520, 410, 380, 300, 240, 180, 120, 90]
    urls, metrics = pages(clicks)
    group = summarise_group("AllUsed_BodyType", urls, metrics)

    assert group.verdict is GroupVerdict.PROMOTE_EXEMPLARS
    assert len(group.exemplars) == 5
    assert group.top_decile_click_share >= 0.7


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


def test_concentration_is_checked_before_low_coverage_exclusion():
    """The gap the spec's decision table left open.

    910 URLs where 10 hub pages take 3,680 clicks has 1.1% coverage, which lands in
    the exclude branch — and excluding it throws away the only pages in the group
    worth indexing. Concentration has to be evaluated first.
    """
    clicks = [0] * 900 + [800, 640, 520, 410, 380, 300, 240, 180, 120, 90]
    urls, metrics = pages(clicks)
    group = summarise_group("AllUsed_BodyType", urls, metrics)
    assert group.verdict is GroupVerdict.PROMOTE_EXEMPLARS
    assert group.coverage < 0.05


def test_concentration_without_volume_is_not_promoted():
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

    assert group.verdict is GroupVerdict.PROMOTE_EXEMPLARS
    assert group.exemplars[0].endswith("p0")


def test_the_facet_override_still_settles_the_diffuse_case():
    """Clicks too thin to concentrate: nothing to promote, so the override decides."""
    clicks = [1] * 70 + [0] * 930
    urls, metrics = pages(clicks, impressions=900)
    group = summarise_group("AllUsed_Fuel", urls, metrics)

    assert group.verdict is GroupVerdict.EXCLUDE
    assert group.overridden
