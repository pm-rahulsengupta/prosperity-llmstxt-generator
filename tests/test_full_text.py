"""The llms-full.txt generator, judged by the rules that grade its output.

These are written against `audit`, not against string shapes. The file scored
12/100 on the first real site checked while every unit test passed, because the
tests asserted what the generator emitted rather than whether the emitted thing
survived the checks. So the central test here generates a document from pages
carrying every defect that was found in the wild and asserts the audit is clean.
"""

from __future__ import annotations

import pytest

from app.core.full_text import hoist_repeated, normalise_body, split_blocks, strip_emphasis
from app.core.models import PageEntry
from app.core.render import render_llms_full
from app.core.rules import audit
from app.core.rules.document import parse_full
from app.core.rules.full_rules import REPEAT_MIN_CHARS, REPEAT_THRESHOLD

# Long enough that FULL-005 counts it, which needs REPEAT_MIN_CHARS characters.
BOILERPLATE = (
    "Whenever I have companies ask me if there are any great agencies in Australia, "
    "I always point them to this team. They have delivered for us consistently and "
    "the reporting is genuinely useful rather than decorative. We have worked with "
    "them across three separate campaigns now and would recommend them without "
    "reservation to anyone weighing up their options."
)


def _page(slug: str, *, markdown: str, title: str = "") -> PageEntry:
    return PageEntry(
        url=f"https://example.com/{slug}",
        title=title or slug.replace("-", " ").title(),
        markdown=markdown,
    )


def _messy_markdown(n: int) -> str:
    """One page shaped the way crawled markdown actually arrives.

    Its own H1, H2 sections, a bold line the extractor read as a heading, trailing
    whitespace, a four-blank-line run, a citation that looks like a page boundary,
    and the site's boilerplate testimonial.

    The filler carries `n` deliberately. Identical filler is *genuinely* repeated
    content, so the hoist strips it from every page and leaves the blocks under
    the FULL-008 floor -- that would be the fixture failing, not the generator.
    Only `BOILERPLATE` is meant to be identical everywhere.
    """
    return (
        f"# Page {n} Title   \n"
        "\n"
        f"Opening paragraph for page {n}. "
        + (f"Body text {n} to give this page weight. " * 40)
        + "\n"
        "\n"
        "## **What is Digital PR?**\n"
        "\n"
        "Digital PR earns coverage. " + (f"More explanation {n} of the discipline. " * 40) + "\n"
        "\n\n\n\n"
        "Source: https://example.com/a-study-we-cited\n"
        "\n"
        "### A deeper heading\t\n"
        "\n"
        "Detail under the deeper heading. " + (f"Supporting detail {n}. " * 40) + "\n"
        "\n"
        f"{BOILERPLATE}\n"
    )


def _generated(pages: list[PageEntry]) -> str:
    return render_llms_full("Example Site", pages)


# -- the test the 12/100 score should have failed -----------------------------


def test_a_generated_file_passes_every_full_rule():
    """The whole point. Seven rules failed on the first real site.

    Each page here carries the defects that were measured there: 83 H1s, 608 H2s
    against 74 sources, 616 body headings, a block repeated 46 times, whitespace
    noise and 834 emphasis-wrapped headings.
    """
    pages = [_page(f"page-{n}", markdown=_messy_markdown(n)) for n in range(8)]

    report = audit("", _generated(pages))
    failed = [f.rule_id for f in report.failures]

    assert failed == [], f"generated llms-full.txt still fails: {failed}"


def test_the_defects_are_really_present_before_the_fix():
    """Guards the test above from passing because the fixture is too clean.

    If `_messy_markdown` ever stops carrying real defects, the test that matters
    keeps passing while proving nothing. This asserts the raw input would fail.
    """
    raw = "# Example Site\n\n---\n\n## Page 0\n\nSource: https://example.com/page-0\n\n" + (
        _messy_markdown(0)
    )

    report = audit("", raw)
    failed = {f.rule_id for f in report.failures}

    assert {"FULL-001", "FULL-003", "FULL-006", "FULL-007"} <= failed


# -- structure ----------------------------------------------------------------


def test_the_document_has_exactly_one_h1():
    doc = parse_full(_generated([_page(f"p{n}", markdown=_messy_markdown(n)) for n in range(4)]))

    assert doc.h1_count == 1


def test_every_h2_is_a_page_boundary_with_a_source():
    """FULL-002 in its own right: the three counts must agree.

    A page's own H2 sections used to arrive as H2s, so 74 pages produced 608 H2s
    and page boundaries could not be located.
    """
    doc = parse_full(_generated([_page(f"p{n}", markdown=_messy_markdown(n)) for n in range(5)]))

    assert doc.h2_count == doc.source_count == len(doc.pages)


def test_body_headings_are_demoted_below_the_boundary():
    doc = parse_full(_generated([_page("p0", markdown=_messy_markdown(0))]))

    levels = {h.level for page in doc.pages for h in page.body_headings}

    assert levels and min(levels) >= 3, "a body heading may not outrank its own page"


def test_relative_hierarchy_survives_demotion():
    """Demote by a shift, not to a fixed level.

    Flattening every body heading to H3 would lose the difference between a
    section and a subsection, which is most of what an outline is for.
    """
    body = normalise_body("# Title\n\n## Section\n\n### Subsection\n\ntext\n")

    assert [line for line in body.splitlines() if line.startswith("#")] == [
        "### Title",
        "#### Section",
        "##### Subsection",
    ]


def test_headings_are_never_promoted():
    """A page whose own headings start at H4 is left alone.

    Raising them would assert a structure the author did not write, and nothing
    in the rules asks for it -- FULL-003 objects to H1 and H2, not to H4.
    """
    body = normalise_body("#### Already deep\n\ntext\n")

    assert "#### Already deep" in body


def test_demotion_stops_at_h6():
    body = normalise_body("# A\n\n###### Deep\n\ntext\n")

    assert "######## " not in body
    assert "###### Deep" in body


# -- emphasis -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("**What is Digital PR?**", "What is Digital PR?"),
        ("*Italic heading*", "Italic heading"),
        ("__Bold underscore__", "Bold underscore"),
        ("***Both***", "Both"),
        ("Plain heading", "Plain heading"),
        ("**Nested *inner* still bold**", "Nested *inner* still bold"),
    ],
)
def test_emphasis_wrapping_is_removed(raw, expected):
    assert strip_emphasis(raw) == expected


def test_a_heading_that_merely_starts_with_an_asterisk_is_escaped_not_stripped():
    """`*args` is not emphasis, but FULL-007 only reads the first character.

    Stripping would silently edit the text; escaping renders identically and
    keeps the rule satisfied.
    """
    assert strip_emphasis("*args and **kwargs") == "\\*args and **kwargs"


# -- whitespace ---------------------------------------------------------------


def test_blank_runs_and_trailing_whitespace_go():
    body = normalise_body("# A   \n\n\n\n\ntext\t\n\n\n\nmore\n")

    assert "\n\n\n" not in body
    assert not [line for line in body.splitlines() if line != line.rstrip()]


def test_code_keeps_its_blank_lines_and_its_hashes():
    """A fence is not prose.

    `# comment` on the first line of a shell example is not a heading, and a gap
    between two functions is formatting rather than noise. Both survive; three
    blank lines, which FULL-006 would flag, do not.
    """
    body = normalise_body(
        "# Title\n\n```python\n# not a heading\ndef a():\n    pass\n\n\ndef b():\n    pass\n```\n"
    )

    assert "# not a heading" in body, "a comment inside a fence is not a document heading"
    assert "pass\n\n\ndef b" in body, "a conventional two-line gap in code is left alone"


# -- boilerplate --------------------------------------------------------------


def test_boilerplate_is_hoisted_once_not_repeated():
    bodies = [f"Unique text for page {n}.\n\n{BOILERPLATE}" for n in range(6)]

    shared, trimmed = hoist_repeated(bodies)

    assert len(shared) == 1
    assert all(BOILERPLATE not in body for body in trimmed)
    assert all(f"Unique text for page {n}." in trimmed[n] for n in range(6))


def test_hoisted_text_still_appears_in_the_file():
    """Hoisting, not deleting. The words stay; the repetition goes."""
    pages = [_page(f"p{n}", markdown=_messy_markdown(n)) for n in range(6)]

    output = _generated(pages)

    assert output.count(BOILERPLATE) == 1


def test_a_block_on_few_pages_is_left_where_it_is():
    """The threshold exists so a coincidence is not treated as boilerplate."""
    bodies = [f"Page {n}.\n\n{BOILERPLATE}" for n in range(REPEAT_THRESHOLD)]

    shared, trimmed = hoist_repeated(bodies)

    assert shared == []
    assert all(BOILERPLATE in body for body in trimmed)


def test_short_repeated_lines_are_not_hoisted():
    """`REPEAT_MIN_CHARS` guards against hoisting things like a repeated heading."""
    short = "Read more" * 3
    assert len(short) < REPEAT_MIN_CHARS
    bodies = [f"Page {n}.\n\n{short}" for n in range(8)]

    shared, _ = hoist_repeated(bodies)

    assert shared == []


def test_code_is_never_hoisted():
    """Two pages documenting the same snippet is not boilerplate.

    Moving a fenced block away from the prose explaining it makes both useless.
    """
    snippet = "```python\n" + ("# a repeated example line\n" * 12) + "```"
    assert len(snippet) >= REPEAT_MIN_CHARS
    bodies = [f"Page {n} explains it.\n\n{snippet}" for n in range(8)]

    shared, trimmed = hoist_repeated(bodies)

    assert shared == []
    assert all(snippet in body for body in trimmed)


def test_a_fence_containing_a_blank_line_is_not_split():
    """Blocks are reassembled after hoisting, so splitting a fence could drop half
    of it and leave the fence unclosed."""
    body = "Intro.\n\n```python\ndef a():\n    pass\n\ndef b():\n    pass\n```\n\nOutro."

    blocks = split_blocks(body)

    assert len(blocks) == 3
    assert blocks[1].count("```") == 2


# -- source lines -------------------------------------------------------------


def test_a_citation_in_the_body_cannot_pose_as_a_page_boundary():
    """A blog post citing a study writes exactly the line this format uses.

    Left alone it inflates `source_count` past the number of pages and breaks
    FULL-002, which is an ERROR.
    """
    doc = parse_full(_generated([_page("p0", markdown=_messy_markdown(0))]))

    assert doc.source_count == 1 == len(doc.pages)


# -- budget -------------------------------------------------------------------


def test_the_default_budget_cannot_produce_a_file_that_fails_its_own_rule():
    """The two constants were 1,000,000 and 800,000 and the gap shipped.

    The first real site produced 249,977 tokens against a 200,000 budget, which
    is precisely the difference between them.
    """
    from app.core.render import DEFAULT_FULL_MAX_CHARS
    from app.core.rules.full_rules import CHARS_PER_TOKEN, DEFAULT_MAX_TOKENS

    assert DEFAULT_FULL_MAX_CHARS <= DEFAULT_MAX_TOKENS * CHARS_PER_TOKEN


def test_truncation_leaves_room_for_saying_it_truncated():
    """The note is appended after the budget is spent, so it has to be reserved
    for -- otherwise stating the omission is what breaks the limit."""
    pages = [_page(f"p{n}", markdown=_messy_markdown(n)) for n in range(12)]

    output = render_llms_full("Example Site", pages, max_chars=6_000)

    assert "omitted" in output
    assert len(output) <= 6_000


def test_a_truncated_file_still_passes_the_rules():
    pages = [_page(f"p{n}", markdown=_messy_markdown(n)) for n in range(12)]

    report = audit("", render_llms_full("Example Site", pages, max_chars=20_000))
    failed = [f.rule_id for f in report.failures]

    assert failed == []


def test_an_empty_heading_is_dropped_not_emitted_bare():
    """`<h3>` wrapping only an image arrives as a heading with no text.

    Emitting it left `#### ` with a trailing space, which was the entire
    remaining FULL-006 failure on a real site after everything else was clean.
    """
    body = normalise_body("# Title\n\n###\n\ntext\n")

    assert "#### " not in body
    assert not [line for line in body.splitlines() if line != line.rstrip()]


def test_an_empty_heading_does_not_set_the_demotion_shift():
    """It is not emitted, so it must not decide how far everything else moves."""
    body = normalise_body("#\n\n## Real section\n\ntext\n")

    assert "### Real section" in body
