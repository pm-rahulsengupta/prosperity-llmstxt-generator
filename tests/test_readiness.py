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


# -- the Layer 1 items that are visible without a browser ---------------------
#
# Three of the seven can be read from the HTML. A static parse is weaker than
# Lighthouse -- it cannot see what CSS or JavaScript does at runtime -- but "no
# <main> anywhere in the document" is a fact, and reporting it as needing a
# browser was giving up on a check we can make. The four that genuinely need
# rendering stay manual, because guessing at them is how a passing score reaches
# a site that fails.


def test_semantic_html_is_read_from_the_markup():
    from app.scrape.readiness import _semantic_html

    good, _ = _semantic_html("<main><nav></nav><article></article></main>")
    bad, detail = _semantic_html("<div><div><div></div></div></div>")

    assert good is CheckState.PASS
    assert bad is CheckState.FAIL
    assert "accessibility tree" in detail


def test_a_clickable_div_without_a_role_is_caught():
    """An agent walking the tree cannot tell it is a button."""
    from app.scrape.readiness import _clickable_divs

    assert _clickable_divs('<div onclick="go()">x</div>')[0] is CheckState.FAIL
    assert _clickable_divs('<div onclick="go()" role="button">x</div>')[0] is CheckState.PASS
    assert _clickable_divs("<button>x</button>")[0] is CheckState.PASS


def test_unlabelled_inputs_are_caught_and_aria_counts():
    from app.scrape.readiness import _form_labels

    assert _form_labels('<label for="e">E</label><input id="e">')[0] is CheckState.PASS
    assert _form_labels('<input id="e" type="text">')[0] is CheckState.FAIL
    assert _form_labels('<input aria-label="Email" type="text">')[0] is CheckState.PASS


def test_hidden_and_submit_inputs_do_not_need_labels():
    from app.scrape.readiness import _form_labels

    assert _form_labels('<input type="hidden" name="t">')[0] is CheckState.NOT_APPLICABLE
    assert _form_labels('<input type="submit" value="Go">')[0] is CheckState.NOT_APPLICABLE


def test_a_page_with_no_inputs_is_not_applicable_rather_than_passing():
    """Nothing to label is not the same as labelling everything."""
    from app.scrape.readiness import _form_labels

    state, detail = _form_labels("<p>Just prose.</p>")
    assert state is CheckState.NOT_APPLICABLE
    assert "no form inputs" in detail


def test_the_four_that_need_rendering_are_still_manual():
    """Layout shift, cursor styles, tap-target size and ghost overlays cannot be
    read from source, and a guess at them would inflate the score."""
    from app.scrape.readiness import STATIC_LAYER1

    layer1 = {i.key for i in CHECKLIST if i.layer == 1}
    assert layer1 - set(STATIC_LAYER1) == {"cls", "cursor", "tap-targets", "overlays"}


def test_a_static_pass_says_it_only_saw_the_homepage():
    """A pass here is evidence about one page, not about the site."""
    import inspect

    from app.scrape import readiness

    assert "(homepage only)" in inspect.getsource(readiness.audit_readiness)
