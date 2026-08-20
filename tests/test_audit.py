"""The audit engine, against real files.

Three fixtures, deliberately: one bad, two good. A rule set tuned against a single
bad file learns to hate that file — the good ones are what stop the rules becoming
a list of things one generator happened to do.

`ours-agency-*` is our own output for prosperitymedia.com.au. The old validator
scored it zero issues, which is what this whole engine exists to fix.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.rules import ALL_RULES, RULES_BY_ID, audit, parse_full, parse_index
from app.core.rules.registry import Outcome, Severity

FILES = Path(__file__).parent / "fixtures" / "files"


@pytest.fixture(scope="module")
def ours() -> tuple[str, str]:
    return (
        (FILES / "ours-agency-llms.txt").read_text(encoding="utf-8"),
        (FILES / "ours-agency-llms-full.txt").read_text(encoding="utf-8"),
    )


@pytest.fixture(scope="module")
def anthropic() -> str:
    return (FILES / "good-saas-docs-llms.txt").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def minimal() -> str:
    return (FILES / "good-minimal-llms.txt").read_text(encoding="utf-8")


# -- the engine itself ------------------------------------------------------


def test_every_rule_has_a_unique_id_and_a_rationale():
    ids = [rule.id for rule in ALL_RULES]
    assert len(ids) == len(set(ids)), "rule ids must be unique -- they are the diff key"
    assert all(rule.rationale for rule in ALL_RULES), "a rule nobody understands gets disabled"


def test_a_skipped_rule_is_not_a_pass():
    """The property the whole score rests on: unchecked must not read as clean."""
    report = audit(
        "# Only an index\n\n> A summary.\n\n## Docs\n\n- [A](https://x.com/a): Words here.\n"
    )
    skipped = {f.rule_id for f in report.skipped}
    assert skipped, "full-file rules should be skipped when there is no full file"
    assert all(f.reason for f in report.skipped), "every skip must say why"
    # And they are out of the denominator, not counted as passes.
    assert not any(f.outcome is Outcome.PASS for f in report.skipped)


def test_nothing_checked_scores_zero_not_one_hundred():
    report = audit("")
    assert report.score == 0
    assert report.applicable_weight == 0


def test_an_error_costs_more_than_an_info():
    no_h1 = audit("Not a heading at all.\n")
    assert no_h1.failed("IDX-001")
    assert no_h1.score < 50


# -- our own file, which the old validator passed ---------------------------


def test_our_own_output_fails_the_defects_it_actually_has(ours):
    index, full = ours
    report = audit(index, full)

    # Each of these is a defect the review brief named, mapped to a rule.
    assert report.failed("IDX-014"), "106 CTA-voice descriptions"
    assert report.failed("IDX-013"), "unverifiable superlatives"
    assert report.failed("IDX-008"), "duplicate titles"
    assert report.failed("IDX-017"), "identity pages stranded in Optional"
    assert report.failed("IDX-015"), "mixed en-AU/en-US spelling"
    assert report.failed("FULL-001"), "80 H1s"
    assert report.failed("FULL-002"), "H2s, Source lines and pages disagree"
    assert report.failed("FULL-003"), "in-body headings never demoted"
    assert report.failed("FULL-005"), "one testimonial repeated 43 times"
    assert report.failed("FULL-009"), "~250k tokens"
    assert report.failed("XF-002"), "144 indexed URLs absent from the full file"

    assert report.score < 60, "a file with this many defects must not look healthy"


def test_the_counts_are_the_real_counts(ours):
    index, full = ours
    report = audit(index, full)
    assert report.by_id("IDX-014").count == 106
    assert report.by_id("FULL-001").count == 80


def test_findings_aggregate_rather_than_flooding(ours):
    """106 banned openers is one finding with a count, not 106 findings."""
    index, full = ours
    report = audit(index, full)
    opener = report.by_id("IDX-014")
    assert opener.count == 106
    assert len(opener.examples) <= 5


# -- the good files, which must not be punished for being good --------------


def test_a_clean_minimal_file_scores_full_marks(minimal):
    """ai-sdk.dev: 10 curated links, all markdown. If this does not score 100 the
    rules are measuring something other than quality."""
    report = audit(minimal)
    assert report.score == 100, [f.message for f in report.failures]


def test_a_large_docs_index_is_not_failed_for_being_large(anthropic):
    """docs.anthropic.com is 58KB with 567 links -- 5.8x the brief's byte budget.

    It is the reference implementation of the format, not a bloated dump, and the
    budget rules must skip rather than guess when no profile says otherwise.
    """
    report = audit(anthropic)
    assert not report.failed("IDX-009"), "byte budget must not apply without a profile"
    assert not report.failed("IDX-010"), "link budget must not apply without a profile"
    assert report.by_id("IDX-009").outcome is Outcome.SKIPPED


def test_a_dash_separator_is_a_description_not_a_broken_link(anthropic):
    """Anthropic writes `- [Title](url) - description`. The spec says `:`, but the
    link is well formed and the description is right there -- parsing only `:` would
    report 567 valid links as malformed with missing descriptions."""
    doc = parse_index(anthropic)
    assert not any(link.malformed for link in doc.links)
    described = [link for link in doc.links if link.has_description]
    # Only 89 of its 567 links carry a description at all -- the rest are bare
    # `- [Title](url)`, which is a real finding rather than a parse failure.
    assert len(described) == 89


def test_prose_bullets_are_not_broken_links(minimal):
    """`- English (en) - 567 pages` is prose in a list, not an attempted link."""
    doc = parse_index(minimal)
    assert not any(link.malformed for link in doc.links)


# -- specific parser behaviour ----------------------------------------------


def test_a_generated_footer_is_not_trailing_content():
    """Our own renderer appends `---` and an italic date line. A rule that failed
    that would fail every file we ship."""
    body = (
        "# Site\n\n> Summary.\n\n## Docs\n\n- [A](https://x.com/a): Words here now.\n"
        "\n---\n*Generated 2026-08-20. Recommend reviewing quarterly.*\n"
    )
    assert not audit(body).failed("IDX-005")


def test_real_trailing_content_is_still_caught():
    body = (
        "# Site\n\n> Summary.\n\n## Docs\n\n- [A](https://x.com/a): Words here now.\n"
        "\nThis is a paragraph of prose that does not belong here at all.\n"
    )
    assert audit(body).failed("IDX-005")


def test_page_boundaries_need_a_source_line(ours):
    """An H2 inside a page body is not a page. Treating every H2 as a boundary
    invents hundreds of empty pages and reports them as missing their Source URL."""
    _, full = ours
    doc = parse_full(full)
    assert len(doc.pages) == 78
    assert doc.source_count == 78
    assert len(doc.orphan_h2s) > 500


def test_an_empty_description_is_seen():
    """The old regex matched `- [T](u): ` with nothing after the colon."""
    body = "# Site\n\n> Summary.\n\n## Docs\n\n- [A](https://x.com/a): \n"
    report = audit(body)
    assert report.failed("IDX-012")


def test_severity_ordering_is_respected():
    assert RULES_BY_ID["IDX-001"].severity is Severity.ERROR
    assert RULES_BY_ID["IDX-015"].severity is Severity.INFO
