"""AGT-001..014 — validating an agents.md.

These judge a different failure from the IDX rules. An llms.txt with a wrong
description is read and shrugged at; an agents.md with a wrong endpoint is acted
on, and the action fails against the client's live site. AGT-004 is therefore the
highest-severity rule in the tool.

Three of the tests below exist because running the validator against our own
generated output failed it 49/100, and two of those three findings were bugs in
the validator rather than in the file.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.rules import audit_agents, render_text

FIXTURES = Path(__file__).parent / "fixtures" / "agents"

SHOP_VERIFIED = [
    "https://s.myshopify.com/api/ucp/mcp",
    "https://shop.example/products.json",
    "https://shop.example/collections.json",
    "https://shop.example/policies/refund",
    "https://shop.example/policies/privacy",
    "https://shop.example/llms.txt",
    "https://shop.example/.well-known/ucp",
]


def shop_report():
    return audit_agents(
        (FIXTURES / "shop.md").read_text(encoding="utf-8"),
        site_url="https://shop.example",
        verified_urls=SHOP_VERIFIED,
        transactional=True,
        content_type="text/markdown",
    )


def bare_report(**overrides):
    kwargs = {
        "site_url": "https://agency.example",
        "verified_urls": [],
        "transactional": False,
        "content_type": "text/markdown",
    }
    kwargs.update(overrides)
    return audit_agents((FIXTURES / "agency_bare.md").read_text(encoding="utf-8"), **kwargs)


# -- our own output has to pass ----------------------------------------------


def test_our_generated_shop_file_is_clean():
    report = shop_report()
    assert not report.failures, [f.message for f in report.failures]
    assert report.score == 100


def test_our_bare_file_is_clean_apart_from_a_true_observation():
    """The agency site publishes no llms.txt, so AGT-013 is correct to fire."""
    report = bare_report()
    assert [f.rule_id for f in report.failures] == ["AGT-013"]


# -- AGT-004: the rule that matters most -------------------------------------


def test_an_invented_endpoint_is_caught():
    body = (FIXTURES / "agency_bare.md").read_text(encoding="utf-8")
    body = body.replace(
        "## Not supported",
        "## Commerce\n\n- Checkout: `https://agency.example/api/ucp/mcp`\n\n## Not supported",
    )
    report = audit_agents(
        body,
        site_url="https://agency.example",
        verified_urls=[],
        transactional=False,
        content_type="text/markdown",
    )

    assert report.failed("AGT-004")
    # And independently by the prose rule, so removing the URL alone is not enough.
    assert report.failed("AGT-005")


def test_a_probe_that_found_nothing_is_evidence_but_one_that_did_not_run_is_not():
    """The distinction the rule initially collapsed.

    Testing `if not verified` treated an empty result as "no probe", silently
    skipping the most important rule on exactly the sites that need it -- the ones
    publishing nothing.
    """
    ran = bare_report(verified_urls=[])
    never = audit_agents(
        (FIXTURES / "agency_bare.md").read_text(encoding="utf-8"),
        site_url="https://agency.example",
        transactional=False,
    )

    assert ran.by_id("AGT-004").outcome.value == "pass"
    assert never.by_id("AGT-004").outcome.value == "skipped"
    assert "no probe results" in never.by_id("AGT-004").reason


def test_the_sites_own_address_is_not_an_unverified_endpoint():
    """It is the thing being described.

    Flagging it made the rule fire on every file we generate, because the identity
    section names the canonical URL.
    """
    report = audit_agents(
        "# Site\n\n## About this site\n\nCanonical site: https://x.example\n\n## Not supported\n\n- none\n",
        site_url="https://x.example",
        verified_urls=[],
        transactional=False,
    )
    assert not report.failed("AGT-004")


# -- AGT-006: privacy, and the regex that cried wolf -------------------------


def test_an_email_address_is_refused():
    report = audit_agents(
        "# Site\n\n## Contact\n\n- Email: merchant@example.com\n",
        site_url="https://x.example",
        verified_urls=[],
    )
    assert report.failed("AGT-006")


def test_a_real_phone_number_is_refused():
    report = audit_agents(
        "# Site\n\n## Contact\n\n- Call (02) 9876 5432 for help.\n",
        site_url="https://x.example",
        verified_urls=[],
    )
    assert report.failed("AGT-006")


@pytest.mark.parametrize(
    "text",
    [
        "Protocol version: 2026-04-08",
        "Supported versions: 2026-01-23, 2026-04-08",
        "*Generated 2026-08-20.*",
        "Up to 2 requests per second.",
    ],
)
def test_dates_and_versions_are_not_mistaken_for_phone_numbers(text):
    """The false positive found by auditing our own file.

    A UCP version string is the most common eight-digit run this file will ever
    contain, and the first pattern matched every one of them -- including the
    generation date in the footer. A validator crying wolf on its own output is
    one nobody keeps running.
    """
    report = audit_agents(
        f"# Site\n\n## About this site\n\n{text}\n", site_url="https://x.example", verified_urls=[]
    )
    finding = report.by_id("AGT-006")
    assert not finding.failed, finding.examples


# -- AGT-009: off-site links --------------------------------------------------


def test_a_platform_endpoint_the_site_declared_is_not_offsite():
    """Shopify's own UCP profile names *.myshopify.com.

    Without this the rule fires on every correctly configured store, flagging the
    platform's canonical endpoint as a possible hijack.
    """
    assert not shop_report().failed("AGT-009")


def test_a_link_to_an_unrelated_host_is_flagged():
    report = audit_agents(
        "# Site\n\n## Read-only\n\n- Data: `https://not-the-site.example/api`\n",
        site_url="https://x.example",
        verified_urls=[],
    )
    assert report.failed("AGT-009")


# -- publishing ---------------------------------------------------------------


def test_a_soft_404_content_type_fails():
    """A 200 of HTML is a page, and agents treat it as not published."""
    report = audit_agents(
        (FIXTURES / "shop.md").read_text(encoding="utf-8"),
        site_url="https://shop.example",
        verified_urls=SHOP_VERIFIED,
        content_type="text/html",
    )
    assert report.failed("AGT-014")


def test_an_unfetched_file_skips_the_publishing_rule():
    assert bare_report(content_type=None).by_id("AGT-014").outcome.value == "skipped"


# -- the rule set will not punish honest absence ------------------------------


def test_a_site_that_publishes_nothing_still_scores_well():
    """A file saying "this site does not transact" is correct, not incomplete.

    Marking it down would push operators toward publishing claims they cannot
    support, which is the exact harm the generator refuses to do.
    """
    assert bare_report().score >= 90


def test_a_missing_not_supported_section_is_flagged():
    report = audit_agents(
        "# Site\n\n## About this site\n\nCanonical site: https://x.example\n",
        site_url="https://x.example",
        verified_urls=[],
    )
    assert report.failed("AGT-010")


def test_claiming_a_specification_is_flagged():
    """agents.md has no ratified spec; claiming conformance overstates."""
    report = audit_agents(
        "# Site\n\n## About\n\nThis file conforms to the agents.md spec.\n",
        site_url="https://x.example",
        verified_urls=[],
    )
    assert report.failed("AGT-011")


# -- scoring stays separate from the llms.txt rules --------------------------


def test_agents_rules_are_not_scored_against_the_llms_txt_denominator():
    """One number covering both documents would describe neither.

    An agents.md judged by the IDX rules would be marked down for having no link
    lines; an llms.txt judged by AGT-004 would be asked for endpoints it was never
    meant to name.
    """
    report = shop_report()
    assert {f.rule_id[:3] for f in report.findings} == {"AGT"}
    assert len(report.findings) == 14


def test_the_report_renders_for_agents_findings():
    """`render_text` looked rules up in the llms.txt map only, and raised."""
    text = render_text(bare_report())
    assert "Score:" in text
    assert "AGT-" in text
