"""The onboarding brief, and what it is and is not allowed to do to a verdict.

The whole point of the floor/ceiling design is that a declaration bounds how far
a verdict may travel without choosing where it lands. Most of these tests exist
to prove the second half of that -- that declaring a pattern valuable does *not*
admit it, which is the failure mode a forced INCLUDE_GROUP would have shipped.
"""

from __future__ import annotations

import pytest

from app.core.metrics import GroupVerdict, PageMetrics, summarise_group
from app.core.onboarding import (
    QUESTIONS,
    Fact,
    SiteBrief,
    brief_from_answers,
    fingerprint,
    has_drifted,
    matches_any,
)


def pages(clicks: list[int], prefix: str = "https://x.com/p", impressions: int | None = None):
    urls = [f"{prefix}{i}" for i in range(len(clicks))]
    metrics = {
        url: PageMetrics(
            url=url,
            clicks=c,
            impressions=impressions if impressions is not None else c * 40,
            source="test",
        )
        for url, c in zip(urls, clicks, strict=True)
    }
    return urls, metrics


# -- pattern matching -------------------------------------------------------


@pytest.mark.parametrize(
    ("candidate", "pattern"),
    [
        ("https://x.com/services/seo/", "/services/*"),
        ("https://x.com/services/seo/technical/", "/services/**"),
        ("https://x.com/services/", "/services/"),
        ("AllNew_Location", "AllNew_*"),
        ("https://x.com/Services/SEO/", "/services/*"),
    ],
)
def test_patterns_match_the_forms_an_operator_actually_types(candidate, pattern):
    assert matches_any(candidate, (pattern,)) == pattern


@pytest.mark.parametrize(
    ("candidate", "pattern"),
    [
        ("https://x.com/blog/post/", "/services/*"),
        ("AllUsed_Make", "AllNew_*"),
        ("https://x.com/services/", "/services/*/deep"),
    ],
)
def test_patterns_do_not_match_what_they_should_not(candidate, pattern):
    assert matches_any(candidate, (pattern,)) is None


def test_the_matched_pattern_is_returned_not_a_boolean():
    """So a rationale can name the answer responsible for the verdict."""
    matched = matches_any("https://x.com/case-studies/acme/", ("/blog/*", "/case-studies/*"))
    assert matched == "/case-studies/*"


# -- the floor --------------------------------------------------------------


def test_declaring_a_pattern_valuable_stops_a_wholesale_exclusion():
    clicks = [0] * 4_000
    urls, metrics = pages(clicks, prefix="https://x.com/services/p")
    brief = SiteBrief(valuable=("/services/*",))

    without = summarise_group("Services", urls, metrics)
    with_brief = summarise_group("Services", urls, metrics, brief=brief)

    assert without.verdict is GroupVerdict.EXCLUDE
    assert with_brief.verdict is GroupVerdict.REVIEW
    assert with_brief.declared == "/services/*"


def test_a_declaration_does_not_force_a_group_in():
    """The distinction the whole design rests on: a floor, not a ceiling.

    4,000 URLs with no demand at all are held for a human to look at. They are
    not admitted, because one careless glob would otherwise put a marketplace's
    entire facet space into the file.
    """
    urls, metrics = pages([0] * 4_000, prefix="https://x.com/services/p")
    group = summarise_group("Services", urls, metrics, brief=SiteBrief(valuable=("/services/*",)))

    assert group.verdict is not GroupVerdict.INCLUDE_GROUP
    assert group.verdict is GroupVerdict.REVIEW


def test_the_floor_keeps_the_exemplars_the_evidence_found():
    """Concentration still decides which pages; the operator only stopped the drop."""
    clicks = [0] * 4_000
    clicks[0], clicks[1] = 150, 40
    urls, metrics = pages(clicks, prefix="https://x.com/services/p", impressions=120)
    group = summarise_group("Services", urls, metrics, brief=SiteBrief(valuable=("/services/*",)))

    assert group.verdict is GroupVerdict.PROMOTE_EXEMPLARS
    assert group.exemplars[0].endswith("p0")


def test_the_floor_beats_the_faceted_signature_override():
    """The rule an operator would most want to argue with has to be reachable.

    A facet-shaped group they have declared valuable is held for review, not
    deleted on a CTR threshold.
    """
    # Shaped so the override is the rule that actually fires: 6% coverage puts the
    # group in REVIEW, and 900k impressions at ~0.007% CTR is what drags it out.
    # A group with no clicks at all would be excluded on coverage and never reach it.
    clicks = [1] * 60 + [0] * 940
    urls, metrics = pages(clicks, prefix="https://x.com/inventory/p", impressions=900)

    undeclared = summarise_group("AllNew_Location", urls, metrics)
    assert undeclared.verdict is GroupVerdict.EXCLUDE
    assert "never chosen" in undeclared.rationale

    group = summarise_group(
        "AllNew_Location", urls, metrics, brief=SiteBrief(valuable=("/inventory/*",))
    )
    assert group.verdict is GroupVerdict.REVIEW
    assert group.declared == "/inventory/*"


def test_an_undeclared_group_is_unaffected_by_someone_elses_declaration():
    urls, metrics = pages([0] * 4_000, prefix="https://x.com/tag/p")
    group = summarise_group("Tags", urls, metrics, brief=SiteBrief(valuable=("/services/*",)))

    assert group.verdict is GroupVerdict.EXCLUDE
    assert group.declared == ""


# -- the ceiling ------------------------------------------------------------


def test_declaring_a_pattern_noise_stops_wholesale_inclusion():
    urls, metrics = pages([12, 40, 8, 30, 55, 9, 21, 3, 17, 44], prefix="https://x.com/tag/p")
    group = summarise_group("Tags", urls, metrics, brief=SiteBrief(noise=("/tag/*",)))

    assert group.verdict is GroupVerdict.PROMOTE_EXEMPLARS
    assert group.declared == "/tag/*"


def test_the_ceiling_does_not_delete_a_page_that_earns_clicks():
    """Its mirror-image constraint: the operator cannot silently drop a winner."""
    urls, metrics = pages([500, 40, 8, 30, 55, 9, 21, 3, 17, 44], prefix="https://x.com/tag/p")
    group = summarise_group("Tags", urls, metrics, brief=SiteBrief(noise=("/tag/*",)))

    assert group.verdict is not GroupVerdict.EXCLUDE
    assert group.exemplars[0].endswith("p0")


# -- the absolutes ----------------------------------------------------------


def test_embargo_excludes_and_nothing_reverses_it():
    """Not evidence to be weighed. A group that would otherwise be included."""
    urls, metrics = pages([12, 40, 8, 30, 55, 9, 21, 3, 17, 44], prefix="https://x.com/ndaclient/p")
    brief = SiteBrief(embargoed=("/ndaclient/*",), valuable=("/ndaclient/*",))
    group = summarise_group("Client", urls, metrics, brief=brief)

    assert group.verdict is GroupVerdict.EXCLUDE
    assert "embargo" in group.rationale.lower()


def test_embargoed_paths_are_never_shown_to_a_model():
    """The disclosure the answer existed to prevent."""
    brief = SiteBrief(
        found_for="SEO", embargoed=("/clients/acquisition-2026/*",), valuable=("/services/*",)
    )
    context = brief.prompt_context()

    assert "acquisition-2026" not in context
    assert "/services/*" in context


def test_must_appear_survives_a_zero_traffic_group():
    urls, metrics = pages([0] * 600, prefix="https://x.com/co/p")
    brief = SiteBrief(must_appear=frozenset({"https://x.com/co/p3"}))
    group = summarise_group("Company", urls, metrics, brief=brief)

    assert group.verdict is GroupVerdict.INCLUDE_GROUP
    assert group.overridden


# -- answers in, brief out --------------------------------------------------


def test_form_input_becomes_a_brief():
    brief = brief_from_answers(
        {
            "found_for": "  Digital PR  ",
            "valuable": "/services/*\n\n/case-studies/*\n/services/*\n",
            "must_appear": ["https://x.com/about/"],
            "facts": {"founded": {"value": "2013", "source": "ASIC registration"}},
        },
        answered_by="rahul@example.com",
    )

    assert brief.found_for == "Digital PR"
    # Blank lines dropped, order kept, duplicate collapsed.
    assert brief.valuable == ("/services/*", "/case-studies/*")
    assert brief.must_appear == frozenset({"https://x.com/about/"})
    assert brief.facts["founded"] == Fact("2013", "ASIC registration")


def test_a_fact_without_provenance_records_who_typed_it():
    brief = brief_from_answers({"facts": {"team_size": "22"}})
    assert brief.facts["team_size"].source == "operator"


def test_an_unanswered_brief_changes_nothing():
    urls, metrics = pages([0] * 4_000)
    plain = summarise_group("g", urls, metrics)
    briefed = summarise_group("g", urls, metrics, brief=SiteBrief())

    assert SiteBrief().is_empty
    assert plain.verdict is briefed.verdict
    assert briefed.declared == ""


# -- drift ------------------------------------------------------------------


def test_a_stable_site_is_not_re_asked():
    groups = ["Services", "Blog", "Case Studies"]
    stamp = fingerprint(groups, 223)
    assert has_drifted(stamp, groups, 230) is None


def test_new_sitemap_groups_trigger_a_re_ask():
    stamp = fingerprint(["Services", "Blog"], 223)
    reason = has_drifted(stamp, ["Services", "Blog", "Inventory"], 223)
    assert reason and "sitemap groups have changed" in reason


def test_a_large_url_swing_triggers_a_re_ask_and_says_which_way():
    groups = ["Services", "Blog"]
    stamp = fingerprint(groups, 200)
    reason = has_drifted(stamp, groups, 900)
    assert reason and "grown" in reason and "900" in reason


def test_a_missing_fingerprint_does_not_nag():
    """A brief written before fingerprinting existed is not evidence of drift."""
    assert has_drifted("", ["Services"], 100) is None


# -- the question set itself ------------------------------------------------


def test_every_question_states_its_consequence():
    """An operator cannot calibrate an answer without knowing what it does."""
    for question in QUESTIONS:
        assert question.effect, question.key
        assert question.key in {f.name for f in SiteBrief.__dataclass_fields__.values()}


def test_the_free_text_questions_say_they_have_no_automatic_effect():
    for question in QUESTIONS:
        if question.kind == "text":
            assert "No automatic effect" in question.effect
