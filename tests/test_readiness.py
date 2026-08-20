"""The agent-readiness audit, against the published checklist.

Twenty-one components, two layers, per-site-type applicability. Most of what is
asserted here is what the score refuses to count, because a readiness number that
quietly ignores a third of its own checklist is the most misleading thing this
tool could produce.
"""

from __future__ import annotations

import pytest

from app.scrape.readiness import (
    CHECKLIST,
    Applicability,
    CheckResult,
    CheckState,
    Priority,
    ReadinessReport,
    SiteType,
)


def result(key: str, state: CheckState) -> CheckResult:
    item = next(i for i in CHECKLIST if i.key == key)
    return CheckResult(item, state)


def report(*results: CheckResult) -> ReadinessReport:
    return ReadinessReport(
        site_url="https://x.example", site_type=SiteType.CONTENT, results=list(results)
    )


# -- the checklist itself -----------------------------------------------------


def test_the_checklist_covers_both_layers():
    layers = {item.layer for item in CHECKLIST}
    assert layers == {1, 2}
    assert len(CHECKLIST) == 21


def test_every_item_states_how_to_verify_it_by_hand():
    """A finding an operator cannot reproduce is one they cannot act on."""
    for item in CHECKLIST:
        assert item.verify, item.key


def test_every_item_has_an_applicability_for_every_site_type():
    for item in CHECKLIST:
        for site_type in SiteType:
            assert site_type in item.applies, (item.key, site_type)


def test_commerce_protocols_are_only_expected_of_shops():
    item = next(i for i in CHECKLIST if i.key == "commerce-protocols")

    assert item.applies[SiteType.ECOMMERCE] is Applicability.CONDITIONAL
    assert item.applies[SiteType.CONTENT] is Applicability.NO
    assert item.applies[SiteType.APP_API] is Applicability.NO


# -- what the score counts ----------------------------------------------------


def test_only_pass_and_fail_move_the_number():
    for state in (CheckState.PASS, CheckState.FAIL):
        assert result("robots", state).scored
    for state in (CheckState.NOT_APPLICABLE, CheckState.MANUAL, CheckState.UNREACHABLE):
        assert not result("robots", state).scored


def test_a_browser_only_check_is_never_counted_as_a_pass():
    """Seven Layer 1 items need Lighthouse. Scoring them as passes because we did
    not look would inflate every report by a third."""
    partial = report(result("robots", CheckState.PASS), result("cls", CheckState.MANUAL))

    assert partial.score == 100
    assert len(partial.checked) == 1
    assert len(partial.manual) == 1
    assert "need a browser" in partial.summary()


def test_an_unreachable_check_is_not_a_failure():
    """A refused request is a fact about us, not about the client's site.

    Nine simultaneous requests to a shared-hosting WordPress site had three
    refused, and those were being reported as the site's shortcomings.
    """
    partial = report(result("robots", CheckState.PASS), result("sitemap", CheckState.UNREACHABLE))
    assert partial.score == 100


def test_a_must_failure_costs_more_than_an_optional_one():
    must_failed = report(result("robots", CheckState.FAIL), result("skills", CheckState.PASS))
    optional_failed = report(result("robots", CheckState.PASS), result("skills", CheckState.FAIL))

    assert must_failed.score < optional_failed.score


def test_failures_are_ordered_by_priority():
    ordered = report(
        result("skills", CheckState.FAIL),
        result("robots", CheckState.FAIL),
        result("content-signals", CheckState.FAIL),
    ).failures

    assert [f.item.priority for f in ordered] == [
        Priority.MUST,
        Priority.SHOULD,
        Priority.OPTIONAL,
    ]


def test_a_report_with_nothing_checkable_scores_zero_rather_than_dividing_by_nothing():
    assert report(result("cls", CheckState.MANUAL)).score == 0


@pytest.mark.parametrize("site_type", list(SiteType))
def test_every_site_type_has_something_to_check(site_type):
    applicable = [i for i in CHECKLIST if i.applies[site_type] is not Applicability.NO]
    assert len(applicable) >= 10, site_type
