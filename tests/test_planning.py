"""The stage-1 planning table, keyed on sitemap provenance.

The correction this encodes: CarsGuide's 11,909 URLs cluster into 397 path
templates that are placeholder soup, while its 167 sitemap names are a clean
taxonomy. The planner was reading the soup. Provenance wins at both extremes --
on a flat WordPress site every URL collapses to one slug template and the
sitemap split is the only signal there is.

Everything here must hold at tier D, with no metrics at all, because that is the
normal state of the tool.
"""

from __future__ import annotations

import pytest

from app.core.metrics import GroupVerdict, PageMetrics
from app.core.onboarding import SiteBrief
from app.core.planning import UNSOURCED, build_planning_table, render_planning_table
from app.llm.stages import _heuristic_intents
from app.scrape.recon import RobotsInfo, SiteRecon


def marketplace() -> SiteRecon:
    """A shape modelled on CarsGuide: a facet space, guides, hubs, an archive."""
    urls: list[str] = []
    sources: dict[str, str] = {}

    def add(count: int, path: str, sitemap: str) -> None:
        for i in range(count):
            url = "https://m.com" + path.format(i=i)
            urls.append(url)
            sources[url] = sitemap

    add(4_000, "/cars/make/{i}", "AllNew_Make.xml")
    add(2_500, "/cars/location/{i}", "AllNew_Location.xml")
    add(60, "/guides/how-to-{i}/part/{i}", "sitemap_buying_guide.xml")
    add(8, "/hub-{i}", "page-sitemap.xml")
    add(300, "/tag/{i}", "tag-sitemap.xml")
    return SiteRecon(site_url="https://m.com", robots=RobotsInfo(), urls=urls, url_sources=sources)


# -- the axis change ---------------------------------------------------------


def test_the_table_is_keyed_on_sitemap_groups_not_path_templates():
    rows = build_planning_table(marketplace())

    assert {r.group_key for r in rows} == {
        "AllNew_Make.xml",
        "AllNew_Location.xml",
        "sitemap_buying_guide.xml",
        "page-sitemap.xml",
        "tag-sitemap.xml",
    }


def test_the_largest_groups_come_first_and_are_named_sitemaps():
    """Ordered by the size of the decision, not alphabetically."""
    rows = build_planning_table(marketplace())

    assert [r.group_key for r in rows[:2]] == ["AllNew_Make.xml", "AllNew_Location.xml"]
    assert rows[0].url_count == 4_000


def test_there_are_far_fewer_groups_than_path_templates():
    """The whole argument for the axis change, asserted on the fixture."""
    from app.scrape.recon import cluster_urls

    recon = marketplace()
    groups = build_planning_table(recon)
    templates = cluster_urls(recon.urls)

    assert len(groups) < len(templates)


# -- tier D: the whole table works with no metrics ---------------------------


def test_every_group_is_review_at_tier_d():
    """No metrics means no evidence, and no evidence excludes nothing."""
    rows = build_planning_table(marketplace())

    assert rows, "table must render without metrics"
    for row in rows:
        assert row.verdict is GroupVerdict.REVIEW
        assert row.confidence == "low"


def test_the_tier_d_columns_are_all_populated_without_credentials():
    rows = build_planning_table(marketplace())
    facets = next(r for r in rows if r.group_key == "AllNew_Make.xml")

    assert facets.url_count == 4_000
    assert facets.template_diversity >= 1
    assert len(facets.sample_urls) == 6


def test_template_diversity_separates_generated_from_editorial():
    """The strongest free signal: it tells the two apart before any click data."""
    rows = {r.group_key: r for r in build_planning_table(marketplace())}
    facets = rows["AllNew_Make.xml"]
    guides = rows["sitemap_buying_guide.xml"]

    assert facets.url_count / facets.template_diversity > 100
    assert guides.url_count / guides.template_diversity < 100


def test_the_sample_spreads_through_the_group():
    """Sitemaps are ordered by date or id, so the first N is the least
    representative slice available."""
    rows = build_planning_table(marketplace())
    sample = next(r for r in rows if r.url_count == 4_000).sample_urls

    assert len(set(sample)) == len(sample)
    assert sample != ["https://m.com/cars/make/" + str(i) for i in range(6)]


# -- the unsourced pseudo-group ----------------------------------------------


def test_unattributed_urls_are_always_held():
    """Not knowing where a page came from is a gap in our knowledge of the site,
    not a fact about the page. Excluding on it turns a discovery failure into a
    deletion."""
    recon = marketplace()
    recon.urls.append("https://m.com/found-by-crawl")

    rows = {r.group_key: r for r in build_planning_table(recon)}

    assert UNSOURCED in rows
    assert rows[UNSOURCED].verdict is GroupVerdict.REVIEW


def test_the_unsourced_group_stays_review_even_when_metrics_say_it_is_dead():
    recon = SiteRecon(
        site_url="https://m.com",
        robots=RobotsInfo(),
        urls=["https://m.com/x" + str(i) for i in range(1_000)],
        url_sources={},
    )
    dead = {
        url: PageMetrics(url=url, clicks=0, impressions=500, source="gsc") for url in recon.urls
    }

    rows = build_planning_table(recon, dead)

    assert rows[0].group_key == UNSOURCED
    assert rows[0].verdict is GroupVerdict.REVIEW


# -- intent is advisory, never a verdict -------------------------------------


def test_intent_alone_can_never_exclude_a_group():
    """The mirror of "declared valuable" never producing INCLUDE_GROUP.

    A classification is a label, not evidence about demand. If a faceted label
    could exclude on its own, the tool would delete a marketplace's inventory on
    the strength of a model reading a sitemap name.
    """
    recon = marketplace()
    rows = build_planning_table(recon, intents=_heuristic_intents(build_planning_table(recon)))

    faceted = [r for r in rows if r.intent == "faceted"]
    assert faceted, "the fixture must contain a group the classifier calls faceted"
    for row in faceted:
        assert row.verdict is not GroupVerdict.EXCLUDE


def test_intent_does_not_change_any_verdict():
    """Same inputs, labels added: verdicts identical."""
    recon = marketplace()
    without = build_planning_table(recon)
    with_intents = build_planning_table(recon, intents=_heuristic_intents(without))

    assert [r.verdict for r in without] == [r.verdict for r in with_intents]


def test_the_heuristic_classifier_needs_no_llm():
    """Tier D again: the label has to exist before any key is configured."""
    intents = _heuristic_intents(build_planning_table(marketplace()))

    assert intents["AllNew_Make.xml"][0] == "faceted"
    assert intents["tag-sitemap.xml"][0] == "utility"
    assert intents["sitemap_buying_guide.xml"][0] == "editorial"
    assert intents["page-sitemap.xml"][0] == "hub"


def test_a_classification_carries_the_evidence_it_used():
    intents = _heuristic_intents(build_planning_table(marketplace()))
    assert "4,000 URLs" in intents["AllNew_Make.xml"][1]


def test_the_classifier_is_never_shown_the_verdicts():
    """Asking separately is pointless if the model can read the answer first.

    It would agree with the metrics and look like corroboration, when the value
    of a second signal is that it can contradict the first.
    """
    from app.llm.prompts.intent import build_user_message

    recon = marketplace()
    dead = {
        url: PageMetrics(url=url, clicks=0, impressions=500, source="gsc") for url in recon.urls
    }
    message = build_user_message(build_planning_table(recon, dead))

    for leak in ("verdict", "exclude", "promote_exemplars", "coverage", "clicks"):
        assert leak not in message.lower()


# -- multi-membership as a promotion signal ----------------------------------


def test_multi_listed_urls_are_counted_not_only_resolved():
    """A URL five sitemaps point at is usually a hub. The collision is settled
    for grouping, but the count is signal in its own right."""
    recon = marketplace()
    hub = recon.urls[-1]
    recon.url_memberships = {hub: 4}

    rows = {r.group_key: r for r in build_planning_table(recon)}

    assert rows[recon.url_sources[hub]].multi_listed == 1


def test_membership_defaults_to_one_when_unknown():
    assert marketplace().multi_listed("https://m.com/anything") == 1


# -- the brief shows through --------------------------------------------------


def test_the_table_names_the_brief_pattern_that_fired():
    """The rationale convention: a verdict traces to the answer that caused it."""
    recon = marketplace()
    dead = {
        url: PageMetrics(url=url, clicks=0, impressions=200, source="gsc") for url in recon.urls
    }
    brief = SiteBrief(valuable=("/cars/location/*",))

    rows = {r.group_key: r for r in build_planning_table(recon, dead, brief=brief)}
    declared = rows["AllNew_Location.xml"]

    assert declared.declared == "/cars/location/*"
    assert "/cars/location/*" in declared.rationale


# -- rendering ----------------------------------------------------------------


def test_the_rendered_table_shows_evidence_beside_the_verdict():
    recon = marketplace()
    rows = build_planning_table(recon, intents=_heuristic_intents(build_planning_table(recon)))
    text = render_planning_table(rows)

    assert "AllNew_Make.xml" in text
    assert "faceted" in text
    assert "TMPL" in text and "MULTI" in text
    assert "5 group(s)" in text


def test_rendering_an_empty_site_does_not_crash():
    empty = SiteRecon(site_url="https://m.com", robots=RobotsInfo())
    assert "No sitemap groups" in render_planning_table(build_planning_table(empty))


@pytest.mark.parametrize("repeat", [1, 2, 3])
def test_the_table_is_deterministic(repeat):
    recon = marketplace()
    first = render_planning_table(build_planning_table(recon))
    for _ in range(repeat):
        assert render_planning_table(build_planning_table(recon)) == first
