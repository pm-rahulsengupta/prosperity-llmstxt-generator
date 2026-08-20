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
    detect_drift,
    matches_any,
    site_shape,
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
    """The evidence still decides which pages; the operator only stopped the drop.

    Two earners in four thousand URLs cannot be measured for concentration, so
    this arrives as a recommendation. The declaration does not upgrade it -- that
    would be the floor manufacturing a conclusion -- but the named pages survive.
    """
    clicks = [0] * 4_000
    clicks[0], clicks[1] = 150, 40
    urls, metrics = pages(clicks, prefix="https://x.com/services/p", impressions=120)
    group = summarise_group("Services", urls, metrics, brief=SiteBrief(valuable=("/services/*",)))

    assert group.verdict is GroupVerdict.REVIEW
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
#
# The first version summed URL counts site-wide with a 20% tolerance, which is
# wrong in both directions on a real property: Gumtree's listing count moves
# further than that on ordinary churn, so it would nag; and one group vanishing
# while another doubles nets to nothing, so a restructure would pass silently.
# Names are the signal and carry zero tolerance; counts are noise and belong
# per-group behind a wide band.


def test_ordinary_publishing_churn_is_not_drift():
    """The false positive that would train an operator to ignore the warning."""
    before = site_shape({"Services": 12, "Blog": 300, "Case Studies": 40})
    after = site_shape({"Services": 12, "Blog": 330, "Case Studies": 44})

    assert not detect_drift(before, after).drifted


def test_a_group_appearing_is_drift_however_small():
    """Zero tolerance on names: a new group means the site was reorganised."""
    drift = detect_drift(site_shape({"Blog": 300}), site_shape({"Blog": 300, "Inventory": 4}))

    assert drift.drifted
    assert drift.added == ("Inventory",)
    assert drift.affected == frozenset({"Inventory"})


def test_a_group_disappearing_is_drift():
    drift = detect_drift(site_shape({"Blog": 300, "Guides": 80}), site_shape({"Blog": 300}))

    assert drift.removed == ("Guides",)
    assert "Guides" in drift.reason()


def test_the_swap_that_a_site_wide_total_would_have_missed():
    """One group gutted, another doubled: the aggregate barely moves.

    This is the false negative that made summing URL counts unusable -- it is
    also the exact shape of a replatform, which is the thing worth catching.
    """
    before = site_shape({"AllNew_Location": 4_000, "Guides": 200})
    after = site_shape({"AllNew_Location": 200, "Guides": 4_000})

    assert sum(before.values()) == sum(after.values())  # a total sees nothing
    drift = detect_drift(before, after)
    assert drift.drifted
    assert {name for name, _, _ in drift.resized} == {"AllNew_Location", "Guides"}


def test_small_groups_do_not_trip_the_count_band():
    """2 URLs to 5 is a 150% change and means nothing.

    Without a floor these would be reported every run until nobody read the
    warnings, which is the same as having no warning.
    """
    drift = detect_drift(site_shape({"Contact": 2}), site_shape({"Contact": 5}))

    assert not drift.drifted


def test_drift_names_the_groups_so_the_action_can_be_narrow():
    """The action is to re-approve what moved, not to invalidate the plan.

    Clearing the plan would discard every human decision on the groups that did
    not move -- the cure being worse than the drift is the whole reason this
    returns names rather than a boolean.
    """
    drift = detect_drift(
        site_shape({"A": 100, "B": 4_000, "C": 60}),
        site_shape({"A": 100, "B": 40, "D": 90}),
    )

    assert drift.affected == frozenset({"B", "C", "D"})
    assert "A" not in drift.affected


def test_a_brief_with_no_recorded_shape_does_not_nag():
    """A brief answered before shape was recorded is not evidence of drift."""
    assert not detect_drift(None, site_shape({"Blog": 300})).drifted
    assert not detect_drift({}, site_shape({"Blog": 300})).drifted


def test_the_shape_round_trips_with_the_brief():
    brief = SiteBrief(valuable=("/a/*",), shape=site_shape({"Blog": 300, "Services": 12}))
    assert SiteBrief.from_dict(brief.to_dict()).shape == brief.shape


# -- the question set itself ------------------------------------------------


def test_every_question_states_its_consequence():
    """An operator cannot calibrate an answer without knowing what it does."""
    for question in QUESTIONS:
        assert question.effect, question.key
        assert question.key in {f.name for f in SiteBrief.__dataclass_fields__.values()}


def test_the_free_text_questions_say_they_have_no_automatic_effect():
    """Only the answers that reach a model and nothing else.

    A `published` answer is prose too, but it is written verbatim into a
    generated file, so it is deterministic and must not claim otherwise. The
    kinds are separate because this invariant is real and folding them would
    quietly break it.
    """
    for question in QUESTIONS:
        if question.kind == "text":
            assert "No automatic effect" in question.effect


def test_a_published_answer_does_not_claim_to_be_inert():
    published = [q for q in QUESTIONS if q.kind == "published"]

    assert published, "the kind exists to describe at least one question"
    for question in published:
        assert "No automatic effect" not in question.effect


# -- the brief reaching the plan stage --------------------------------------
#
# These exist because an earlier version of this wiring silently did not apply:
# the prompt gained an unused import, ruff removed it, and every test still
# passed. Asserting on the rendered prompt is what makes that visible.


def test_the_plan_prompt_carries_what_the_operator_said():
    from app.llm.prompts.plan import build_user_message

    brief = SiteBrief(
        found_for="Digital PR and link building",
        audience="Marketing managers evaluating agencies",
    )
    message = build_user_message("223 crawlable URLs", 400, brief)

    assert "Digital PR and link building" in message
    assert "Marketing managers evaluating agencies" in message


def test_the_plan_prompt_withholds_the_url_patterns():
    """They are enforced in code. Asking a model for them invites it to differ."""
    from app.llm.prompts.plan import build_user_message

    brief = SiteBrief(embargoed=("/clients/acquisition-2026/*",))
    message = build_user_message("223 crawlable URLs", 400, brief)

    assert "acquisition-2026" not in message


def test_an_absent_brief_leaves_the_plan_prompt_as_it_was():
    from app.llm.prompts.plan import build_user_message

    assert build_user_message("brief", 400) == build_user_message("brief", 400, SiteBrief())


# -- the form ---------------------------------------------------------------


def test_the_brief_form_renders_every_question_with_its_consequence():
    """Rendering catches what a route test would, without a database or a login.

    A missing variable or a renamed field is a 500 on a page the operator hits
    before their first run, which is the worst possible place to find it.
    """
    from jinja2 import StrictUndefined

    from app.main import _brief_form_values, templates

    # The app's own environment, because `base.html` reads globals registered on
    # it. Building a fresh one would test a template that does not exist. Only
    # `undefined` is swapped, so a variable the route forgets to pass raises here
    # instead of rendering as empty.
    env = templates.env
    previous, env.undefined = env.undefined, StrictUndefined
    try:
        html = env.get_template("brief.html").render(
            request=None,
            user=None,
            domain="prosperitymedia.com.au",
            questions=QUESTIONS,
            answers=_brief_form_values(
                SiteBrief(valuable=("/services/*",), found_for="Digital PR")
            ),
            run_id="abc",
            drift_reason=None,
            metrics={},
            imported=None,
            import_notes=None,
            gsc_enabled=False,
        )
    finally:
        env.undefined = previous

    for question in QUESTIONS:
        assert question.prompt in html
        assert question.effect in html
    # Stored answers come back, so a re-confirmation is an edit and not a re-entry.
    assert "/services/*" in html
    assert "Digital PR" in html


def test_stored_answers_survive_a_round_trip_through_the_form():
    from app.main import _brief_form_values, _parse_facts

    original = SiteBrief(
        found_for="Digital PR",
        valuable=("/services/*", "/case-studies/*"),
        must_appear=frozenset({"https://x.com/about/"}),
        facts={"founded": Fact("2013", "ASIC registration")},
    )
    values = _brief_form_values(original)
    values["facts"] = _parse_facts(values["facts"])
    again = brief_from_answers(values)

    assert again.valuable == original.valuable
    assert again.must_appear == original.must_appear
    assert again.facts == original.facts


def test_a_fact_line_without_a_source_still_parses():
    from app.main import _parse_facts

    parsed = _parse_facts("founded = 2013\nteam_size = 22 = LinkedIn\nnonsense line\n")

    assert parsed["founded"] == {"value": "2013", "source": "operator"}
    assert parsed["team_size"] == {"value": "22", "source": "LinkedIn"}
    assert "nonsense line" not in parsed


# -- embargo scope ----------------------------------------------------------
#
# Pinned deliberately: embargo means *never crawled and never stored*, not
# merely absent from the output. A page withheld for legal or confidentiality
# reasons whose body sits in Postgres is not what the operator was promised, and
# retrofitting the stronger meaning later is a data-deletion job.


def test_embargoed_urls_are_removed_before_the_fetch():
    from app.core.onboarding import split_embargoed

    urls = [
        "https://x.com/",
        "https://x.com/clients/acquisition-2026/brief/",
        "https://x.com/clients/acquisition-2026/timeline/",
        "https://x.com/services/",
    ]
    kept, suppressed = split_embargoed(urls, SiteBrief(embargoed=("/clients/acquisition-2026/*",)))

    assert kept == ["https://x.com/", "https://x.com/services/"]
    assert suppressed == {"/clients/acquisition-2026/*": 2}


def test_suppression_is_counted_per_pattern_so_it_can_be_reported():
    """Hidden from the model is not the same as hidden from the operator.

    The planner can propose an embargoed group in good faith and never be told
    it was overruled, so without a count the page simply vanishes and nobody can
    answer why.
    """
    from app.core.onboarding import split_embargoed

    urls = ["https://x.com/a/1", "https://x.com/a/2", "https://x.com/b/1"]
    _, suppressed = split_embargoed(urls, SiteBrief(embargoed=("/a/*", "/b/*")))

    assert suppressed == {"/a/*": 2, "/b/*": 1}


def test_no_embargo_means_no_filtering_and_no_noise():
    from app.core.onboarding import split_embargoed

    urls = ["https://x.com/", "https://x.com/services/"]
    assert split_embargoed(urls, SiteBrief()) == (urls, {})
    assert split_embargoed(urls, None) == (urls, {})


def test_shrinking_is_as_detectable_as_growing():
    """Percentage change cannot see a shrink.

    A group can grow without limit but only ever shrink by 100%, so a threshold
    at or above 1.0 makes gutting a section invisible while doubling one is
    caught. Fold-change treats them as the same size of event.
    """
    gutted = detect_drift(site_shape({"Guides": 4_000}), site_shape({"Guides": 200}))
    grown = detect_drift(site_shape({"Guides": 200}), site_shape({"Guides": 4_000}))

    assert gutted.drifted and grown.drifted
    assert gutted.resized == (("Guides", 4_000, 200),)
    assert "shrank" in gutted.reason()
    assert "grew" in grown.reason()


# -- run actions ------------------------------------------------------------


def test_the_run_page_renders_its_actions():
    """Same guard as the brief form: a missing variable here is a 500 on the
    page an operator uses most."""
    from types import SimpleNamespace

    from jinja2 import StrictUndefined

    from app.db.models import RunStatus
    from app.main import templates

    env = templates.env
    previous, env.undefined = env.undefined, StrictUndefined
    try:
        html = env.get_template("run.html").render(
            request=None,
            user=SimpleNamespace(email="a@b.c", is_admin=True),
            run=SimpleNamespace(
                id="r1",
                domain="example.com",
                site_url="https://example.com",
                status="complete",
                pattern="agency_services",
                max_pages=400,
                site_name="",
                site_summary="",
                notes="",
                llmstxt="",
                llms_full="",
                issues=[],
                stats={},
                error="",
                sitemap_html=10,
                sitemap_total=10,
                indexed_estimate=10,
                size_tier="small",
                size_warnings=[],
                created_at=None,
                plan_source="llm",
            ),
            plan=SimpleNamespace(rules=[], site_pattern="agency_services", reasoning=""),
            pages=[],
            events=[],
            status=RunStatus.COMPLETE,
        )
    finally:
        env.undefined = previous

    assert "Re-run from the start" in html
    assert "Delete permanently" in html
    # Terminal run, so cancel is not offered.
    assert 'action="/runs/r1/cancel"' not in html


def test_delete_is_hidden_from_a_non_admin():
    from types import SimpleNamespace

    from app.db.models import RunStatus
    from app.main import templates

    html = templates.env.get_template("run.html").render(
        request=None,
        user=SimpleNamespace(email="a@b.c", is_admin=False),
        run=SimpleNamespace(
            id="r1",
            domain="example.com",
            site_url="https://example.com",
            status="complete",
            pattern="",
            max_pages=400,
            site_name="",
            site_summary="",
            notes="",
            llmstxt="",
            llms_full="",
            issues=[],
            stats={},
            error="",
            sitemap_html=10,
            sitemap_total=10,
            indexed_estimate=10,
            size_tier="small",
            size_warnings=[],
            created_at=None,
            plan_source="llm",
        ),
        plan=SimpleNamespace(rules=[], site_pattern="", reasoning=""),
        pages=[],
        events=[],
        status=RunStatus.COMPLETE,
    )

    assert "Delete permanently" not in html
    # Re-running is safe, so it stays available to everyone.
    assert "Re-run from the start" in html


# -- the primary action -------------------------------------------------------
#
# The single most useful answer in the brief: the one thing no amount of crawling
# reveals. Two sites can be built identically and want opposite things from an
# agent, and only the operator knows which.


def test_the_stated_goal_beats_a_detected_platform():
    """A WooCommerce install on a site whose goal is enquiries is a fact about the
    build, not about the business. Only the second belongs in an instruction file.
    """
    from app.core.agents_doc import profile_for
    from app.core.ranking import PATTERN_AGENCY, PATTERN_ECOMMERCE_RETAIL

    assert profile_for("contact_agency", platform_sells=True) == PATTERN_AGENCY
    assert profile_for("shop_on_store") == PATTERN_ECOMMERCE_RETAIL


def test_a_detected_shop_is_used_only_when_nobody_has_said_otherwise():
    from app.core.agents_doc import profile_for
    from app.core.ranking import PATTERN_ECOMMERCE_RETAIL

    assert profile_for("", platform_sells=True) == PATTERN_ECOMMERCE_RETAIL


def test_knowing_nothing_falls_back_to_the_shape_that_cannot_transact():
    from app.core.agents_doc import profile_for
    from app.core.ranking import PATTERN_AGENCY

    assert profile_for("") == PATTERN_AGENCY


def test_only_the_buying_goals_are_transactional():
    from app.core.onboarding import TRANSACTIONAL_ACTIONS, PrimaryAction

    assert PrimaryAction.SHOP_ON_STORE in TRANSACTIONAL_ACTIONS
    assert PrimaryAction.FIND_LOCAL_INVENTORY in TRANSACTIONAL_ACTIONS
    for action in (
        PrimaryAction.CONTACT_LOCAL,
        PrimaryAction.CONTACT_AGENCY,
        PrimaryAction.BOOK_APPOINTMENT,
        PrimaryAction.READ_AND_CITE,
        PrimaryAction.USE_THE_API,
    ):
        assert action not in TRANSACTIONAL_ACTIONS


def test_an_unrecognised_answer_is_undecided_rather_than_a_guess():
    from app.core.onboarding import PrimaryAction, brief_from_answers

    assert brief_from_answers({"primary_action": "nonsense"}).primary_action is (
        PrimaryAction.UNDECIDED
    )
    assert brief_from_answers({}).primary_action is PrimaryAction.UNDECIDED


def test_every_action_maps_to_a_profile():
    """A goal with no shape behind it would silently fall back to the default."""
    from app.core.agents_doc import ACTION_PROFILES
    from app.core.onboarding import ACTION_LABELS

    for action in ACTION_LABELS:
        assert action.value in ACTION_PROFILES, action


def test_the_action_round_trips_through_storage():
    from app.core.onboarding import PrimaryAction, SiteBrief

    brief = SiteBrief(primary_action=PrimaryAction.FIND_LOCAL_INVENTORY)
    assert SiteBrief.from_dict(brief.to_dict()).primary_action is PrimaryAction.FIND_LOCAL_INVENTORY
