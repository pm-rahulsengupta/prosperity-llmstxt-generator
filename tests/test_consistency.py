"""The consistency audit, and an honest account of what it cannot do.

The audit exists because the onboarding `facts` question has been promising it
since it was written. These tests pin both halves of the promise: that a
contradiction is found, and that an absence is *not* reported as one.

One test is an `xfail`. Negation is invisible to token containment, and a
limitation recorded only in a docstring is a limitation nobody re-reads. Putting
it in the suite means it shows up in every run as a known gap rather than as a
surprise the first time a client's page says "we do not offer X".
"""

from __future__ import annotations

import pytest

from app.core.consistency import (
    CONTAINMENT_FLOOR,
    Support,
    audit_facts,
    build_corpus,
    check_claim,
)
from app.core.models import PageEntry
from app.core.onboarding import Fact


def page(url: str, markdown: str, title: str = "") -> PageEntry:
    return PageEntry(url=url, title=title or url, markdown=markdown)


ABOUT = """
# About Prosperity Media

Prosperity Media is a Sydney digital PR and SEO agency founded in 2013.
The team specialises in earning editorial coverage for Australian retail and
finance brands, and in technical search consulting.

Our head office is in Surry Hills and we work with clients across Australia.
"""

SERVICES = """
# What we do

We run digital PR campaigns, technical SEO audits, and content strategy
engagements. Our link acquisition work focuses on tier-one Australian
publishers rather than directory placements.
"""


def corpus():
    return build_corpus(
        [page("https://x.example/about", ABOUT), page("https://x.example/services", SERVICES)]
    )


# -- the two verdicts that must never be confused ------------------------------


def test_a_claim_the_site_makes_is_supported():
    verdict = check_claim("Prosperity Media specialises in earning editorial coverage.", corpus())

    assert verdict.support is Support.SUPPORTED
    assert verdict.url.endswith("/about")


def test_a_claim_the_site_never_mentions_is_absent_not_contradicted():
    """The distinction the whole module turns on.

    A site that does not state its staff count has not denied one. Reporting
    that as a contradiction would make the audit cry wolf, and an audit that
    cries wolf is one an operator turns off.
    """
    verdict = check_claim("The agency employs forty-two staff across three offices.", corpus())

    assert verdict.support is Support.ABSENT
    assert verdict.support is not Support.CONTRADICTED


def test_a_founding_year_that_disagrees_is_a_contradiction():
    """The commonest real defect, and the only thing asserted with confidence."""
    verdict = check_claim("Prosperity Media was founded in 2011.", corpus())

    assert verdict.support is Support.CONTRADICTED
    assert verdict.conflicts
    conflict = verdict.conflicts[0]
    assert conflict.claim_value == "2011"
    assert conflict.corpus_value == "2013"
    assert "/about" in conflict.url, "the operator needs to know which page disagrees"


def test_a_matching_founding_year_is_not_a_contradiction():
    assert check_claim("Founded in 2013.", corpus()).support is not Support.CONTRADICTED


def test_a_year_that_is_not_a_founding_date_is_ignored():
    """A blog post mentioning 2019 is not a claim about when the company started."""
    body = build_corpus([page("https://x.example/blog", "# Blog\n\nOur 2019 campaign won awards.")])

    verdict = check_claim("The company was founded in 2013.", body)

    assert verdict.support is not Support.CONTRADICTED


# -- the guards against matching everything ------------------------------------


def test_a_generic_sentence_matches_nothing():
    """Without the rare-token requirement this scores perfectly against any page."""
    verdict = check_claim("We deliver services to our clients.", corpus())

    assert verdict.support is Support.ABSENT


def test_boilerplate_repeated_across_pages_supports_nothing():
    """`hoist_repeated` strips it before the corpus is built.

    A testimonial on forty-six pages is evidence of nothing, and left in it would
    match almost any claim about the business.
    """
    testimonial = (
        "Whenever companies ask me if there are any great agencies in Australia, "
        "I always point them to this exceptional team who consistently deliver "
        "outstanding measurable results for every single client engagement."
    )
    pages = [
        page(f"https://x.example/p{n}", f"# Page {n}\n\nUnique body {n}.\n\n{testimonial}")
        for n in range(8)
    ]

    verdict = check_claim(testimonial, build_corpus(pages))

    assert verdict.support is Support.ABSENT


def test_vocabulary_scattered_down_a_page_does_not_support_a_claim():
    """The one-block requirement.

    Otherwise a claim can be assembled from words that appear on the page but
    never together, which is how a paraphrase of nothing gets marked supported.
    """
    scattered = build_corpus(
        [
            page(
                "https://x.example/x",
                "# Heading\n\nWe mention kubernetes here.\n\nAnd separately, "
                "somewhere else entirely, we talk about veterinary pricing.\n\n"
                "A third block about unrelated matters.",
            )
        ]
    )

    verdict = check_claim("Kubernetes veterinary pricing is our speciality.", scattered)

    assert verdict.support is Support.ABSENT


# -- quotes --------------------------------------------------------------------


def test_a_real_quote_is_found():
    verdict = check_claim(
        "The agency is based in Sydney.",
        corpus(),
        prefer="p01",
        quote="Sydney digital PR and SEO agency founded in 2013",
    )

    assert verdict.quote_found is True


def test_a_fabricated_quote_is_not_found():
    """The cheap half of the audit: a span either is in the page or is not.

    This is what turns "does this sentence follow from the corpus" into a
    substring search, and it is the only part of the check that is exact.
    """
    verdict = check_claim(
        "The agency was named agency of the year.",
        corpus(),
        prefer="p01",
        quote="named Australian agency of the year three times running",
    )

    assert verdict.quote_found is False


def test_a_quote_survives_reflowing_and_emphasis():
    verdict = check_claim(
        "Head office location.",
        corpus(),
        quote="Our **head office**   is in\nSurry Hills",
    )

    assert verdict.quote_found is True


# -- empty and degenerate inputs ------------------------------------------------


def test_an_empty_corpus_is_uncheckable_not_absent():
    """Nothing to check against is a fact about us, not about the claim.

    The same distinction the probes draw between `unreachable` and a 404.
    """
    verdict = check_claim("Anything at all.", build_corpus([]))

    assert verdict.support is Support.UNCHECKABLE


def test_pages_with_no_markdown_are_skipped():
    assert build_corpus([page("https://x.example/a", "")]).empty


def test_the_corpus_is_deterministic():
    first, second = corpus(), corpus()

    assert [d.id for d in first.docs] == [d.id for d in second.docs]
    assert [d.blocks for d in first.docs] == [d.blocks for d in second.docs]


# -- the promise the onboarding UI makes ----------------------------------------


def test_stated_facts_are_audited_against_the_site():
    verdicts = audit_facts(
        type("Brief", (), {"facts": {"founded": Fact("2011", "operator")}})(), corpus()
    )

    assert verdicts["founded"].support is Support.CONTRADICTED


def test_a_fact_the_site_does_not_mention_is_absent():
    """Not blocking. The onboarding copy implies it is, and that copy is wrong."""
    verdicts = audit_facts(
        type("Brief", (), {"facts": {"team_size": Fact("22", "LinkedIn")}})(), corpus()
    )

    assert verdicts["team_size"].support is Support.ABSENT


def test_a_brief_with_no_facts_audits_to_nothing():
    assert audit_facts(type("Brief", (), {"facts": {}})(), corpus()) == {}


# -- the known gap ---------------------------------------------------------------


@pytest.mark.xfail(
    reason=(
        "Negation is invisible to token containment: 'we do not offer X' and "
        "'we offer X' share almost every content token. Recorded here rather "
        "than in a docstring so it appears in every run as a known gap."
    ),
    strict=True,
)
def test_negation_is_not_detected():
    denied = build_corpus(
        [
            page(
                "https://x.example/services",
                "# Services\n\nWe do not offer paid media management or "
                "programmatic advertising buying for our retail clients.",
            )
        ]
    )

    verdict = check_claim(
        "We offer paid media management and programmatic advertising buying "
        "for our retail clients.",
        denied,
    )

    assert verdict.support is not Support.SUPPORTED


def test_the_containment_floor_is_a_deliberate_number():
    """Pinned so a future tweak is a decision rather than a drift."""
    assert 0.5 < CONTAINMENT_FLOOR <= 1.0


def test_the_onboarding_copy_matches_what_the_audit_does():
    """The copy promised something the code did not do, for months.

    It said a *missing* fact "becomes a blocking question rather than an
    invention". Absence does not block and should not: a site that never states
    its founding year has not denied one. Now that the audit exists, the promise
    and the behaviour have to agree, and this is what keeps them agreeing.
    """
    from app.core.onboarding import QUESTIONS

    facts = next(q for q in QUESTIONS if q.key == "facts")

    assert "contradicts" in facts.effect, "the copy must name the case that blocks"
    assert "blocking question" not in facts.effect, "absence does not block"
