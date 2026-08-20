"""The AI-assisted onboarding wizard.

Thirteen empty fields is a form nobody finishes. GEO Tracker's wizard header
records the same conclusion -- it replaced a four-step wizard with one call that
pre-fills everything for review -- and this is that pattern.

The model proposes; code verifies; a person decides. Every test here is about one
of those three boundaries holding.
"""

from __future__ import annotations

import pytest

from app.llm.prompts.onboard import SYSTEM, build_user_message, keep_matching, parse, schema

SITE_URLS = [
    "https://x.com/seo-agency/",
    "https://x.com/seo-brisbane/",
    "https://x.com/blog/a-post/",
    "https://x.com/tag/seo/",
]


# -- the schema OpenAI will actually accept -----------------------------------


def test_every_property_is_required():
    """Structured outputs rejects a schema whose `required` omits any property.

    The rejection arrives as a 400 the client swallows into its heuristic
    fallback, so the feature silently does nothing instead of failing visibly --
    which is how it shipped broken the first time.
    """
    spec = schema()
    assert set(spec["properties"]) == set(spec["required"])


def test_the_action_enum_matches_the_briefs_own_actions():
    """A value the brief cannot parse would be dropped to undecided in silence."""
    from app.core.onboarding import PrimaryAction

    allowed = set(schema()["properties"]["primary_action"]["enum"])
    assert allowed == {a.value for a in PrimaryAction}


# -- what the model is not asked -----------------------------------------------


@pytest.mark.parametrize("withheld", ["ai_bot_policy", "embargoed", "mcp_server_url"])
def test_decisions_with_consequences_are_not_delegated(withheld):
    """Three fields a model has no basis for.

    Whether a client permits AI training is commercial and legal, and on
    Cloudflare refusing it also costs Googlebot. Nothing in a crawl reveals what
    is under NDA. An invented MCP server would be published as fact.
    """
    assert withheld not in schema()["properties"]


def test_the_prompt_asks_for_blanks_rather_than_plausible_guesses():
    assert "rather than a plausible guess" in SYSTEM
    assert "adopted without being checked" in SYSTEM


def test_the_prompt_warns_that_sitemap_names_are_not_paths():
    """The first live run proposed `/post-sitemap1.xml` as a path pattern."""
    assert "NOT paths" in SYSTEM
    assert "post-sitemap1.xml" in SYSTEM


# -- the guard: patterns must match something ---------------------------------


def test_a_pattern_matching_nothing_is_dropped():
    """Measured live: the model proposed `/seo-perth/*` for a site whose
    /seo-perth/ returns 404, alongside Brisbane and Darwin which return 200."""
    kept, dropped = keep_matching(["/seo-agency/*", "/seo-perth/*"], SITE_URLS)

    assert kept == ["/seo-agency/*"]
    assert dropped == ["/seo-perth/*"]


def test_a_sitemap_filename_is_dropped():
    kept, dropped = keep_matching(["/post-sitemap1.xml", "/blog/*"], SITE_URLS)

    assert kept == ["/blog/*"]
    assert "/post-sitemap1.xml" in dropped


def test_real_patterns_survive():
    kept, dropped = keep_matching(["/blog/*", "/tag/*"], SITE_URLS)

    assert set(kept) == {"/blog/*", "/tag/*"}
    assert not dropped


def test_the_guard_needs_the_full_url_list_not_a_sample():
    """Checking against a 40-URL sample dropped `/seo-agency/*` on a site that
    has one, because the model also reads the homepage and proposes patterns for
    pages outside the sample. The guard then did more damage than the defect."""
    sample = SITE_URLS[:1]
    everything = SITE_URLS

    assert keep_matching(["/tag/*"], sample)[0] == []
    assert keep_matching(["/tag/*"], everything)[0] == ["/tag/*"]


# -- parsing into what the form posts ------------------------------------------


def test_lists_become_the_newline_text_the_form_uses():
    """One parser for a suggestion and a typed answer, so only one can be wrong."""
    out = parse({"valuable": ["/a/*", "/b/*"], "primary_action": "contact_agency"})

    assert out["valuable"] == "/a/*\n/b/*"
    assert out["primary_action"] == "contact_agency"


def test_missing_fields_come_back_empty_rather_than_absent():
    out = parse({})

    for key in ("primary_action", "found_for", "audience", "valuable", "noise"):
        assert out[key] == ""


def test_the_reasoning_is_carried_for_the_reviewer():
    assert parse({"reasoning": "because the site sells"})["_reasoning"] == "because the site sells"


# -- the evidence shown to the model -------------------------------------------


def test_the_message_carries_the_sitemap_evidence():
    message = build_user_message(
        "https://x.com", "An agency", "wordpress", [("post-sitemap", 91)], SITE_URLS
    )

    assert "post-sitemap" in message
    assert "91" in message
    assert "wordpress" in message


def test_the_existing_brief_is_not_shown_to_the_model():
    """A model given the current answers agrees with them, and a proposal that
    agrees with what is already there tells the consultant nothing."""
    import inspect

    source = inspect.getsource(build_user_message)
    assert "brief" not in source.replace("the brief", "").lower() or "not shown" in source
