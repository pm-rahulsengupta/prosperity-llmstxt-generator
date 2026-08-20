"""URL canonicalisation: the join the whole metrics layer depends on.

The UTM defect was not mainly a click-splitting bug. The damaging half was
silent -- a tagged URL never joined its sitemap entry, so its clicks were
dropped, coverage understated demand, and the metric recommended excluding a
page that earns. A wrong answer at full confidence.

Every pair below is a way that same join fails. Each asserts merge or non-merge
explicitly, because "these two are different pages" is as much a claim as the
other and getting it wrong deletes real pages from a marketplace index.
"""

from __future__ import annotations

import pytest

from app.core.metrics import (
    CanonicalPolicy,
    PageMetrics,
    canonical_metric_url,
    merge_metrics,
)

# (left, right, should_merge, why)
PAIRS = [
    ("https://x.com/seo-melbourne", "https://x.com/seo-melbourne/", True, "trailing slash"),
    ("https://x.com/SEO-Melbourne/", "https://x.com/seo-melbourne/", True, "path case"),
    ("https://www.x.com/a/", "https://x.com/a/", True, "www"),
    ("http://x.com/a/", "https://x.com/a/", True, "scheme"),
    ("https://x.com/a/#section", "https://x.com/a/", True, "fragment"),
    ("https://x.com/caf%C3%A9/", "https://x.com/café/", True, "percent-encoding"),
    ("https://x.com/a/?utm_source=g", "https://x.com/a/", True, "utm"),
    ("https://x.com/a/?gclid=1", "https://x.com/a/", True, "gclid"),
    ("https://x.com/a/?fbclid=1", "https://x.com/a/", True, "fbclid"),
    ("https://x.com/a/?msclkid=1", "https://x.com/a/", True, "msclkid"),
    ("https://x.com/a/?mc_cid=1", "https://x.com/a/", True, "mc_cid"),
    ("https://x.com/a/?ref=nav", "https://x.com/a/", True, "ref"),
    ("https://x.com/a/?", "https://x.com/a/", True, "empty query"),
    ("https://x.com/a/?b=1&", "https://x.com/a/?b=1", True, "trailing ampersand"),
    ("https://x.com/a/?b=2&a=1", "https://x.com/a/?a=1&b=2", True, "param order"),
    ("https://x.com/a/?a=1&a=1", "https://x.com/a/?a=1", True, "duplicate param"),
    # Non-merges matter just as much.
    ("https://x.com/a/?page=2", "https://x.com/a/", False, "pagination is a real page"),
    ("https://x.com/a/?sort=price", "https://x.com/a/", False, "sort is a real page"),
    ("https://x.com/a/", "https://x.com/b/", False, "different paths"),
    ("https://x.com/a/?q=Sydney", "https://x.com/a/?q=sydney", False, "query case is not folded"),
]


@pytest.mark.parametrize(("left", "right", "merges", "why"), PAIRS)
def test_the_pathological_pairs(left, right, merges, why):
    assert (canonical_metric_url(left) == canonical_metric_url(right)) is merges, why


# -- properties determinism depends on ---------------------------------------


@pytest.mark.parametrize("url", [pair[0] for pair in PAIRS] + [pair[1] for pair in PAIRS])
def test_canonicalisation_is_idempotent(url):
    """f(f(x)) == f(x).

    Without this, a URL that has already been through the function can come out
    different the second time, and rows canonicalised at ingest would stop
    matching rows canonicalised at query time.
    """
    once = canonical_metric_url(url)
    assert canonical_metric_url(once) == once


def test_merging_is_order_independent():
    """GSC pagination order is not guaranteed stable across requests.

    If merge order changed the result, two runs over identical data could
    disagree, and the group verdict with them.
    """
    rows = [
        PageMetrics(url="https://x.com/a/", clicks=10, impressions=100, source="gsc"),
        PageMetrics(
            url="https://www.x.com/a/?utm_source=g", clicks=5, impressions=50, source="gsc"
        ),
        PageMetrics(url="http://x.com/A/", clicks=1, impressions=9, source="gsc"),
        PageMetrics(url="https://x.com/b/", clicks=7, impressions=70, source="gsc"),
    ]

    forward = merge_metrics(rows)
    backward = merge_metrics(list(reversed(rows)))
    shuffled = merge_metrics([rows[2], rows[0], rows[3], rows[1]])

    for other in (backward, shuffled):
        assert set(other) == set(forward)
        for url in forward:
            assert other[url].clicks == forward[url].clicks
            assert other[url].impressions == forward[url].impressions

    # All three spellings of /a/ collapsed into one.
    assert forward[canonical_metric_url("https://x.com/a/")].clicks == 16


# -- the per-site allowlist ---------------------------------------------------


def test_a_site_can_declare_a_tracking_looking_param_meaningful():
    """`ref` is tracking on most sites and a real parameter on some.

    A global rule cannot be right for both, which is why the allowlist is per
    site rather than a constant someone edits and breaks for everyone else.
    """
    policy = CanonicalPolicy(meaningful_params=frozenset({"ref"}))

    assert canonical_metric_url("https://x.com/a/?ref=nav") == canonical_metric_url(
        "https://x.com/a/"
    )
    assert canonical_metric_url("https://x.com/a/?ref=nav", policy) != canonical_metric_url(
        "https://x.com/a/", policy
    )


def test_the_property_host_decides_which_way_www_folds():
    """Plenty of properties *are* the www host; always stripping it is wrong."""
    policy = CanonicalPolicy(canonical_host="www.x.com")

    assert canonical_metric_url("https://x.com/a/", policy) == "https://www.x.com/a"
    assert canonical_metric_url("https://www.x.com/a/", policy) == "https://www.x.com/a"


def test_an_unrelated_host_is_not_folded_into_the_property():
    """The fold is www-versus-apex, not "make every host match"."""
    policy = CanonicalPolicy(canonical_host="x.com")
    assert "other.com" in canonical_metric_url("https://other.com/a/", policy)


# -- the orphan counter: the defence against the *next* unknown join failure ---


def test_orphans_are_counted_by_rows_and_by_clicks():
    """Both, because either alone lies.

    Ten thousand orphaned rows earning nothing is tidy-up. Ten orphaned rows
    holding a fifth of the traffic is a broken join wearing a small row count
    as a disguise.
    """
    from app.core.metrics import join_metrics

    known = ["https://x.com/a/", "https://x.com/b/"]
    metrics = merge_metrics(
        [
            PageMetrics(url="https://x.com/a/", clicks=10, source="gsc"),
            PageMetrics(url="https://x.com/b/", clicks=10, source="gsc"),
            PageMetrics(url="https://x.com/ghost/", clicks=80, source="gsc"),
        ]
    )
    report = join_metrics(known, metrics)

    assert report.orphan_rows == 1
    assert report.orphan_share == pytest.approx(1 / 3)
    # One row in three, but four clicks in five.
    assert report.orphan_click_share == pytest.approx(0.8)
    assert report.looks_broken


def test_a_clean_join_does_not_look_broken():
    from app.core.metrics import join_metrics

    known = ["https://x.com/a/", "https://x.com/b/"]
    metrics = merge_metrics(
        [
            PageMetrics(url="https://www.x.com/a/?utm_source=g", clicks=10, source="gsc"),
            PageMetrics(url="http://x.com/B/", clicks=5, source="gsc"),
        ]
    )
    report = join_metrics(known, metrics)

    assert report.orphan_rows == 0
    assert not report.looks_broken


def test_the_orphan_sample_is_ranked_by_clicks():
    """The orphans worth diagnosing are the ones carrying traffic."""
    from app.core.metrics import join_metrics

    metrics = merge_metrics(
        [PageMetrics(url=f"https://x.com/g{i}/", clicks=i, source="gsc") for i in range(10)]
    )
    report = join_metrics(["https://x.com/known/"], metrics, sample_size=3)

    assert report.orphan_sample == (
        canonical_metric_url("https://x.com/g9/"),
        canonical_metric_url("https://x.com/g8/"),
        canonical_metric_url("https://x.com/g7/"),
    )


def test_the_known_side_is_canonicalised_too():
    """A sitemap spells URLs its own way; the join has to meet in the middle."""
    from app.core.metrics import join_metrics

    metrics = merge_metrics([PageMetrics(url="https://x.com/a", clicks=10, source="gsc")])
    report = join_metrics(["https://www.x.com/A/"], metrics)

    assert report.orphan_rows == 0


def test_no_metrics_is_not_a_broken_join():
    """Tier D is the normal state of the tool, not an error to flag."""
    from app.core.metrics import JoinReport, join_metrics

    assert not join_metrics(["https://x.com/a/"], {}).looks_broken
    assert JoinReport().orphan_share == 0.0
    assert "No metric rows" in JoinReport().summary()
