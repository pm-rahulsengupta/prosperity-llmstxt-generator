"""CRW, HDR and CAT — the rules for the three artifacts nothing used to check.

These judge files a human has touched. `robots.txt` in particular is the only
artifact the tool does not own end to end: it generates a block the operator
pastes into a file that already exists, so every interesting failure is a
property of the merge and the generator cannot see it.
"""

from __future__ import annotations

from app.core.rules import audit_catalog, audit_crawl, audit_headers

# -- CRW-001, the rule the crawl set exists for -------------------------------

CONTRADICTORY = """User-agent: OAI-SearchBot
Disallow: /

User-agent: *
Allow: /
Content-Signal: ai-train=no, search=yes, ai-input=yes
"""

CONSISTENT = """User-agent: OAI-SearchBot
Allow: /

User-agent: GPTBot
Disallow: /

User-agent: *
Allow: /
Content-Signal: ai-train=no, search=yes, ai-input=yes
Sitemap: https://x.example/sitemap.xml
"""


def test_a_signal_that_contradicts_the_rules_fails():
    """The site owner believes they opted into AI search. They have not.

    Silent, common, and invisible to every other check the tool runs.
    """
    report = audit_crawl(CONTRADICTORY, fetched=True)

    assert report.failed("CRW-001")
    finding = report.by_id("CRW-001")
    assert "OAI-SearchBot" in finding.examples[0]
    assert report.capped_by == "error"


def test_a_consistent_policy_passes():
    report = audit_crawl(CONSISTENT, fetched=True)

    assert not report.failed("CRW-001")
    assert not report.failed("CRW-007")


def test_consecutive_agent_lines_share_the_directives_beneath_them():
    """The piece of robots.txt syntax hand-merging gets wrong most often."""
    shared = """User-agent: OAI-SearchBot
User-agent: PerplexityBot
Disallow: /

User-agent: *
Allow: /
Content-Signal: ai-train=no, search=yes, ai-input=yes
"""
    report = audit_crawl(shared, fetched=True)

    assert report.failed("CRW-001")
    assert report.by_id("CRW-001").count == 2, "both agents are blocked, not just the first"


def test_blocking_googlebot_is_the_expensive_accident():
    """An AI policy that takes organic search with it is an outage."""
    report = audit_crawl("User-agent: Googlebot\nDisallow: /\n", fetched=True)

    assert report.failed("CRW-007")


def test_an_allow_beside_a_disallow_is_not_a_block():
    """Allow wins for the same path in every major implementation."""
    report = audit_crawl("User-agent: Googlebot\nDisallow: /\nAllow: /\n", fetched=True)

    assert not report.failed("CRW-007")


def test_a_generated_block_is_not_judged_as_a_whole_file():
    """It is an addition to a file we never see, so the rules about the
    catch-all and about search crawlers are not its to answer."""
    report = audit_crawl("User-agent: GPTBot\nDisallow: /\n", fetched=False)

    assert report.by_id("CRW-006").outcome.value == "skipped"
    assert report.by_id("CRW-007").outcome.value == "skipped"


def test_a_trailing_user_agent_with_no_rules_is_inert():
    """Genuinely empty: nothing follows it at all.

    Note the case this is *not* testing. A blank line between two User-agent
    lines does not end the group -- per RFC 9309 they still share the directives
    beneath, so `User-agent: GPTBot` above a blank line and `User-agent: *` with
    `Allow: /` means GPTBot is allowed, not inert. That surprises people, and it
    is what the parser implements.
    """
    report = audit_crawl("User-agent: *\nAllow: /\n\nUser-agent: GPTBot\n", fetched=True)

    assert report.failed("CRW-004")
    assert "GPTBot" in report.by_id("CRW-004").examples


def test_a_blank_line_does_not_end_a_user_agent_group():
    """The spec behaviour, pinned because it looks like a bug when you meet it."""
    report = audit_crawl("User-agent: GPTBot\n\nUser-agent: *\nAllow: /\n", fetched=True)

    assert not report.failed("CRW-004"), "GPTBot shares the Allow beneath it"


def test_a_partial_signal_leaves_permissions_undefined():
    report = audit_crawl("Content-Signal: search=yes\n", fetched=True)

    assert report.failed("CRW-002")
    assert "ai-train" in " ".join(report.by_id("CRW-002").examples)


def test_a_relative_sitemap_is_discarded_by_crawlers():
    report = audit_crawl("Sitemap: /sitemap.xml\n", fetched=True)

    assert report.failed("CRW-008")


def test_the_published_policy_is_compared_against_the_brief():
    stated_no_search = audit_crawl(
        "Content-Signal: ai-train=no, search=yes, ai-input=yes\n",
        intended_policy="block_all",
        fetched=True,
    )

    assert stated_no_search.failed("CRW-009")


def test_no_stated_policy_means_no_verdict():
    """The file and the intent disagreeing is a question for a person."""
    report = audit_crawl("Content-Signal: ai-train=no, search=yes, ai-input=yes\n")

    assert report.by_id("CRW-009").outcome.value == "skipped"


# -- HDR ----------------------------------------------------------------------

HEADERS = """# Cloudflare Pages _headers
/*
  Link: </sitemap.xml>; rel="sitemap"
  Link: </llms.txt>; rel="describedby"; type="text/markdown"
"""


def test_a_link_to_a_file_that_was_not_generated_fails():
    """The thesis of the whole file: a Link pointing at a 404 is worse than
    no Link, because it costs a request and teaches distrust."""
    report = audit_headers(HEADERS, artifacts={"robots.txt"}, site_url="https://x.example")

    assert report.failed("HDR-001")
    assert "/llms.txt" in report.by_id("HDR-001").examples[0]


def test_a_link_to_a_generated_file_passes():
    report = audit_headers(
        HEADERS, artifacts={"llms.txt", "robots.txt"}, site_url="https://x.example"
    )

    assert not report.failed("HDR-001")


def test_no_bundle_means_no_verdict_rather_than_a_pass():
    """ "We cannot see what was generated" is not "nothing was generated"."""
    report = audit_headers(HEADERS, site_url="https://x.example")

    assert report.by_id("HDR-001").outcome.value == "skipped"


def test_a_wrong_declared_type_is_a_wasted_fetch():
    report = audit_headers(
        '/*\n  Link: </llms.txt>; rel="describedby"; type="application/json"\n',
        artifacts={"llms.txt"},
        site_url="https://x.example",
    )

    assert report.failed("HDR-003")


def test_a_duplicate_rel_is_undefined_behaviour():
    report = audit_headers(
        '/*\n  Link: </a.txt>; rel="describedby"\n  Link: </b.txt>; rel="describedby"\n',
        artifacts=set(),
        site_url="https://x.example",
    )

    assert report.failed("HDR-004")


def test_headers_with_no_path_pattern_are_inert():
    report = audit_headers(
        '  Link: </llms.txt>; rel="describedby"\n', artifacts=set(), site_url="https://x.example"
    )

    assert report.failed("HDR-006")


# -- CAT ----------------------------------------------------------------------

GOOD_CATALOG = """{
  "specVersion": "1.0",
  "entries": [
    {"identifier": "urn:air:x.example:doc:llms", "url": "https://x.example/llms.txt", "type": "text/markdown"},
    {"identifier": "urn:air:x.example:api:openapi", "url": "https://x.example/openapi.json", "type": "application/vnd.oai.openapi+json"}
  ]
}"""


def test_a_well_formed_catalog_passes():
    report = audit_catalog(GOOD_CATALOG, artifacts=set(), site_url="https://x.example")

    assert report.failures == []


def test_an_entry_pointing_off_site_fails():
    """Software connects to whatever this lists."""
    off_site = GOOD_CATALOG.replace("https://x.example/openapi.json", "https://elsewhere.example/x")
    report = audit_catalog(off_site, artifacts=set(), site_url="https://x.example")

    assert report.failed("CAT-001")


def test_duplicate_identifiers_make_references_ambiguous():
    duped = GOOD_CATALOG.replace("urn:air:x.example:api:openapi", "urn:air:x.example:doc:llms")
    report = audit_catalog(duped, artifacts=set(), site_url="https://x.example")

    assert report.failed("CAT-004")


def test_a_one_entry_catalog_is_not_worth_publishing():
    """`worth_publishing` refuses below two, so a published one means somebody
    edited it by hand."""
    single = '{"specVersion": "1.0", "entries": [{"identifier": "urn:air:x.example:doc:a", "url": "https://x.example/a", "type": "text/markdown"}]}'
    report = audit_catalog(single, artifacts=set(), site_url="https://x.example")

    assert report.failed("CAT-006")


def test_malformed_json_is_reported_rather_than_crashing():
    report = audit_catalog("{not json", artifacts=set(), site_url="https://x.example")

    assert report.failed("CAT-002")
    assert report.by_id("CAT-004").outcome.value == "skipped", "later rules skip, not crash"


def test_an_unrecognised_media_type_is_flagged():
    odd = GOOD_CATALOG.replace("text/markdown", "application/x-invented")
    report = audit_catalog(odd, artifacts=set(), site_url="https://x.example")

    assert report.failed("CAT-007")


# -- the sets are wired -------------------------------------------------------


def test_the_new_rule_ids_are_distinct_from_the_existing_ones():
    """A collision would make `render_text` look a rule up in the wrong table."""
    from app.core.rules import AGENTS_BY_ID, RULES_BY_ID
    from app.core.rules.crawl_rules import CRAWL_BY_ID
    from app.core.rules.delivery_rules import CATALOG_BY_ID, HEADER_BY_ID

    tables = [RULES_BY_ID, AGENTS_BY_ID, CRAWL_BY_ID, HEADER_BY_ID, CATALOG_BY_ID]
    seen: set[str] = set()
    for table in tables:
        assert not (seen & set(table)), seen & set(table)
        seen |= set(table)
