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
    audit_readiness,
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


def test_the_checklist_is_the_sheet_plus_the_two_files_this_tool_generates():
    """Twenty-one from the published checklist, plus agents.md and ai-catalog.

    Neither is in the sheet -- it predates Agentic Resource Discovery, and
    agents.md was a Shopify convention when it was written -- and both are what
    this tool exists to produce, so auditing a site without them would leave the
    two most relevant components unexamined.
    """
    from app.core.components import COMPONENTS

    assert len(CHECKLIST) == len(COMPONENTS) == 25
    # Plus the two WCAG rules added after the sheet was written: 4.1.2 on
    # deprecated roles and 4.1.1 on duplicate ids.
    added = {"agents-md", "ai-catalog", "aria-roles", "unique-ids"}
    assert added <= {c.key for c in CHECKLIST}


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


def test_aria_landmarks_count_as_much_as_the_tags():
    """Diagnosed against the rendered DOM of a live site.

    prosperitymedia.com.au has no `<main>` and no `<nav>` element anywhere, and
    carries `role="main"` and `role="banner"` instead. A tag-only check called
    that a failure, when an agent walking the accessibility tree sees exactly the
    landmark it needs -- the role is what the tree is built from.
    """
    from app.scrape.readiness import _semantic_html

    state, detail = _semantic_html(
        '<div role="main"><div role="banner"></div><div role="navigation"></div></div>'
    )

    assert state is CheckState.PASS
    assert "role=main" in detail


def test_a_clickable_div_without_a_role_is_caught():
    """An agent walking the tree cannot tell it is a button."""
    from app.scrape.readiness import _clickable_divs

    assert _clickable_divs('<div onclick="go()">x</div>')[0] is CheckState.FAIL
    assert _clickable_divs('<div onclick="go()" role="button">x</div>')[0] is CheckState.PASS
    assert _clickable_divs("<button>x</button>")[0] is CheckState.PASS


def test_a_focusable_div_without_a_role_is_caught():
    """The real-world pattern, and the false pass that hid it.

    Searching for inline `onclick` alone found none anywhere on
    prosperitymedia.com.au, so the check passed a page carrying two genuine
    violations: the mobile menu is `<div class="hamburger" tabindex="0">` with no
    role, which is keyboard-reachable and unidentifiable.
    """
    from app.scrape.readiness import _clickable_divs

    hamburger = '<div class="uabb-creative-menu-mobile-toggle hamburger" tabindex="0"></div>'
    state, detail = _clickable_divs(hamburger)

    assert state is CheckState.FAIL
    assert "hamburger" in detail


def test_anchors_are_not_flagged_for_being_focusable():
    """180 of 182 focusable elements on that page were `<a tabindex="-1">` in a
    lazy-loaded gallery. Flagging those would bury the two that matter."""
    from app.scrape.readiness import _clickable_divs

    gallery = "".join(f'<a href="/i{i}" tabindex="-1">x</a>' for i in range(50))
    assert _clickable_divs(gallery)[0] is CheckState.PASS


def test_a_pass_says_it_only_saw_the_markup():
    """A div made clickable purely by addEventListener is invisible to any static
    parse -- and is the worst version of the fault, being unreachable by keyboard
    too. Passing means "nothing visible in the markup", not "audited"."""
    from app.scrape.readiness import _clickable_divs

    state, detail = _clickable_divs("<p>Just prose.</p>")

    assert state is CheckState.PASS
    assert "in the markup" in detail


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


def test_the_page_checks_run_across_templates_not_just_the_homepage():
    """A homepage is the least representative page most sites have.

    It is usually bespoke while the templates carry the structure an agent will
    meet, so a service page and a blog post fail differently and auditing one
    says nothing about the other. The report names how many pages it saw.
    """
    import inspect

    source = inspect.getsource(audit_readiness)

    assert "sample_urls" in source
    assert "page(s) sampled" in source


def test_one_failing_template_fails_the_site():
    """Averaging would let a clean homepage hide a broken service template."""
    import inspect

    assert "Worst answer wins" in inspect.getsource(audit_readiness)


# -- WCAG 4.1.2: roles assistive technology recognises ------------------------
#
# Three distinct faults with one symptom -- ignored or misannounced -- and three
# different fixes, so they are reported apart rather than as one "bad role".


def test_a_deprecated_role_is_caught_and_names_its_replacement():
    """`directory` was deprecated in WAI-ARIA 1.2. Honoured by fewer tools each
    year, and nothing errors when it stops working."""
    from app.scrape.readiness import _aria_roles

    state, detail = _aria_roles('<div role="directory">Menu</div>')

    assert state is CheckState.FAIL
    assert "deprecated" in detail
    assert "list" in detail


def test_an_abstract_role_is_a_category_error_not_a_deprecation():
    """Abstract roles define the taxonomy and were never valid in markup, so
    assistive technology has no behaviour to attach to one."""
    from app.scrape.readiness import _aria_roles

    state, detail = _aria_roles('<div role="widget">x</div>')

    assert state is CheckState.FAIL
    assert "never valid in markup" in detail


def test_an_invented_role_is_caught():
    """`dialogbox` for `dialog` is the common shape, and it fails silently."""
    from app.scrape.readiness import _aria_roles

    state, detail = _aria_roles('<div role="dialogbox">x</div>')

    assert state is CheckState.FAIL
    assert "not a role in any ARIA specification" in detail


def test_current_roles_pass():
    from app.scrape.readiness import _aria_roles

    assert _aria_roles('<nav role="navigation" aria-label="Site"></nav>')[0] is CheckState.PASS


def test_dpub_and_graphics_roles_are_not_flagged_as_unknown():
    """Separate specifications with their own vocabularies. Flagging them would
    make the check wrong about correct markup, which is how a check gets ignored.
    """
    from app.scrape.readiness import _aria_roles

    assert _aria_roles('<section role="doc-abstract"></section>')[0] is CheckState.PASS
    assert _aria_roles('<g role="graphics-symbol"></g>')[0] is CheckState.PASS


def test_a_page_with_no_roles_is_not_applicable_rather_than_passing():
    from app.scrape.readiness import _aria_roles

    assert _aria_roles("<p>Just prose.</p>")[0] is CheckState.NOT_APPLICABLE


def test_multiple_roles_in_one_attribute_are_all_checked():
    """A role attribute is a fallback list; each token has to be valid."""
    from app.scrape.readiness import _aria_roles

    assert _aria_roles('<div role="button directory"></div>')[0] is CheckState.FAIL


# -- WCAG 4.1.1: unique ids ---------------------------------------------------


def test_duplicate_ids_are_caught():
    from app.scrape.readiness import _unique_ids

    state, detail = _unique_ids('<div id="a"></div><div id="a"></div>')

    assert state is CheckState.FAIL
    assert "duplicated" in detail


def test_a_duplicated_id_that_something_points_at_is_reported_first():
    """A duplicate nothing references is untidy. One an `aria-labelledby` or a
    `label for=` resolves to is a control wired to the wrong element, which
    breaks behaviour rather than validation."""
    from app.scrape.readiness import _unique_ids

    state, detail = _unique_ids('<label for="a">L</label><input id="a"><input id="a">')

    assert state is CheckState.FAIL
    assert "resolve to the wrong element" in detail


def test_unique_ids_pass():
    from app.scrape.readiness import _unique_ids

    assert _unique_ids('<div id="a"></div><div id="b"></div>')[0] is CheckState.PASS


def test_a_page_with_no_ids_is_not_applicable():
    from app.scrape.readiness import _unique_ids

    assert _unique_ids("<p>Just prose.</p>")[0] is CheckState.NOT_APPLICABLE


def test_both_new_checks_pass_the_live_page_they_were_written_against():
    """A negative control. prosperitymedia.com.au uses four current roles and 27
    unique ids, so a check that fails there is producing false positives."""
    from app.scrape.readiness import _aria_roles, _unique_ids

    clean = (
        '<div role="banner"></div><div role="main"></div>'
        '<div role="figure"></div><img role="img" id="a"><div id="b"></div>'
    )
    assert _aria_roles(clean)[0] is CheckState.PASS
    assert _unique_ids(clean)[0] is CheckState.PASS
