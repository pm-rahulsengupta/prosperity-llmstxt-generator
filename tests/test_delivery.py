"""Sorting a handover's problems by who has to act on them.

The engine that finds them is tested elsewhere. What is tested here is the split:
a bundle whose only failures are about the client's site is sendable, and one
whose failures are about our own rendering is not.
"""

from __future__ import annotations

from app.core.delivery import check_delivery

GOOD_INDEX = """# Example

> An example company that sells examples.

## Pages

- [About](https://example.com/about): Who we are and what we do here
- [Contact](https://example.com/contact): How to reach the team by phone or email
"""

GOOD_FULL = (
    """# Example

> An example company that sells examples.

---

## About

Source: https://example.com/about

"""
    + ("Body text that is long enough to count as a real page. " * 120).strip()
    + """

---

## Contact

Source: https://example.com/contact

"""
    + ("Body text that is long enough to count as a real page. " * 120).strip()
)

GOOD_AGENTS = """# Example

> An example company.

## About this site

Canonical site: https://example.com

Content overview for language models: https://example.com/llms.txt

## Not supported

- This site does not transact.

_Last updated 2026-09-09._
"""

FILES = {
    "llms.txt": GOOD_INDEX,
    "llms-full.txt": GOOD_FULL,
    "agents.md": GOOD_AGENTS,
    "robots.txt": "User-agent: *\nAllow: /\n",
}


def test_a_clean_bundle_is_sendable():
    report = check_delivery(
        llms_txt=GOOD_INDEX,
        llms_full=GOOD_FULL,
        agents_md=GOOD_AGENTS,
        expected_files=FILES,
        generate_full=True,
    )
    assert report.sendable, [i.title for i in report.defects]


def test_a_named_but_empty_file_is_caught():
    """An operator reads the list of names, not the sizes. A file that is present
    and empty reads as delivered and is not."""
    files = {**FILES, "agents.md": "   "}
    report = check_delivery(
        llms_txt=GOOD_INDEX, llms_full=GOOD_FULL, expected_files=files, generate_full=True
    )

    assert not report.sendable
    assert any("agents.md is present but empty" in i.title for i in report.defects)


def test_a_missing_file_is_caught():
    files = {k: v for k, v in FILES.items() if k != "robots.txt"}
    report = check_delivery(
        llms_txt=GOOD_INDEX,
        llms_full=GOOD_FULL,
        agents_md=GOOD_AGENTS,
        expected_files=files,
        generate_full=True,
    )

    assert any("robots.txt is missing" in i.title for i in report.defects)


def test_llms_full_without_the_blockquote_is_our_defect_not_the_clients():
    """XF-001. The two files are one claim about one organisation, so a full file
    with no summary is a rendering fault and must not be sent."""
    full = GOOD_FULL.replace("> An example company that sells examples.\n\n", "")
    report = check_delivery(
        llms_txt=GOOD_INDEX, llms_full=full, agents_md=GOOD_AGENTS, expected_files=FILES
    )

    assert not report.sendable
    assert any(i.rule_id == "XF-001" for i in report.defects)
    assert all(i.remedy for i in report.defects if i.rule_id == "XF-001")


def test_a_stated_truncation_is_a_limit_and_a_silent_one_is_a_defect():
    """XF-002 against FULL-009's budget: on any large site the two cannot both be
    satisfied, and the cap should win. The verdict turns on whether the file says
    so, not on the arithmetic -- a truncation the file states leaves the client
    knowing what they have; the same truncation unannounced claims to be the whole
    site."""
    index = GOOD_INDEX + "\n- [Missing](https://example.com/missing): A page not in the full file\n"

    silent = check_delivery(llms_txt=index, llms_full=GOOD_FULL, expected_files=FILES)
    assert any(i.rule_id == "XF-002" for i in silent.defects)

    stated = check_delivery(
        llms_txt=index,
        llms_full=GOOD_FULL
        + "\n---\n\n*1 lower-priority page(s) omitted to keep this file under 800,000 characters.*\n",
        expected_files=FILES,
    )
    assert any(i.rule_id == "XF-002" for i in stated.limits)
    assert not any(i.rule_id == "XF-002" for i in stated.defects)


def test_the_clients_own_content_problems_do_not_block_the_send():
    """Duplicate titles are the audit doing its job. They belong in the
    deliverable, not in the way of it."""
    index = GOOD_INDEX + "- [About](https://example.com/about-us): Who we are and what we do\n"
    report = check_delivery(llms_txt=index, llms_full=GOOD_FULL, expected_files=FILES)

    assert any(i.rule_id == "IDX-008" for i in report.findings)
    assert not any(i.rule_id == "IDX-008" for i in report.defects)


def test_a_fallback_is_stated_rather_than_left_in_the_stats():
    report = check_delivery(
        llms_txt=GOOD_INDEX,
        llms_full=GOOD_FULL,
        expected_files=FILES,
        run_stats={"llm": {"fallbacks": ["triage: invalid JSON: Expecting value"]}},
    )

    assert any("triage stage fell back" in i.title for i in report.limits)


def test_javascript_only_pages_are_reported_as_a_finding_about_the_site():
    """The pages are absent from the file, and the reason is a fact about the
    client's site that an AI crawler hits the same way we did."""
    report = check_delivery(
        llms_txt=GOOD_INDEX,
        llms_full=GOOD_FULL,
        expected_files=FILES,
        run_stats={"fetch": {"js_only": 3, "js_only_urls": ["https://example.com/a"]}},
    )

    assert any("need JavaScript" in i.title for i in report.findings)


def test_a_few_named_pages_missing_is_a_finding_and_most_of_them_is_a_defect():
    """Five of 173 absent is two pages no sitemap lists and three that need
    JavaScript. Ninety of 173 absent is the selection logic failing."""
    must = {f"https://example.com/p{i}" for i in range(10)}
    index = GOOD_INDEX + "\n".join(
        f"- [P{i}](https://example.com/p{i}): Page {i} of the example site" for i in range(9)
    )

    few = check_delivery(
        llms_txt=index, llms_full=GOOD_FULL, expected_files=FILES, must_appear=must
    )
    assert any("you named are not in the file" in i.title for i in few.findings)

    many = check_delivery(
        llms_txt=GOOD_INDEX, llms_full=GOOD_FULL, expected_files=FILES, must_appear=must
    )
    assert any("you named are not in the file" in i.title for i in many.defects)


def test_the_summary_says_what_each_pile_is_for():
    report = check_delivery(llms_txt=GOOD_INDEX, llms_full=GOOD_FULL, expected_files=FILES)
    text = report.summary()
    assert text
    if report.defects:
        assert "fix before sending" in text


def test_the_section_renders_all_three_lists():
    """The split has to survive into the page, not just the dataclass. An operator
    reading one merged list cannot tell what blocks the send."""
    from jinja2 import Environment, FileSystemLoader, StrictUndefined

    report = check_delivery(
        llms_txt=GOOD_INDEX,
        llms_full=GOOD_FULL.replace("> An example company that sells examples.\n\n", ""),
        agents_md=GOOD_AGENTS,
        expected_files=FILES,
        run_stats={
            "llm": {"fallbacks": ["triage: invalid JSON"]},
            "fetch": {"js_only": 2, "js_only_urls": ["https://example.com/a"]},
        },
    )

    env = Environment(loader=FileSystemLoader("templates"), undefined=StrictUndefined)
    html = env.get_template("partials/delivery.html").render(delivery=report, domain="example.com")

    assert "Fix before sending" in html
    assert "Report to the client" in html
    assert "State in the handover" in html
    assert "Not ready" in html
    assert "XF-001" in html
    assert "need JavaScript" in html
    assert "triage stage fell back" in html
    # The file table is what catches a named-but-empty artifact.
    assert "robots.txt" in html
