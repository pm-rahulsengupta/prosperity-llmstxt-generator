"""End-to-end: Screaming Frog CSV to a spec-compliant llms.txt.

No network, no LLM key. This is the exit criterion for the core port -- if this
suite passes, the tool produces a valid file on its own and every LLM stage is a
bonus rather than a dependency.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from app.core.csv_source import parse_screaming_frog_csv
from app.core.pipeline import FilterOptions, GenerateOptions, generate, rebuild
from app.core.ranking import PATTERN_CATALOG
from app.core.validate import validate

GOLDEN = Path(__file__).parent / "fixtures" / "golden_llms.txt"
SITE = "https://example.com"

# Injected so the output is reproducible. The source read the clock inside the
# renderer, which is also where it crashed.
FIXED_DATE = date(2026, 8, 20)


def _run(sf_csv: str, **overrides):
    options = GenerateOptions(
        pattern=PATTERN_CATALOG,
        site_name="Example",
        site_summary="The platform teams use to ship software faster.",
        generated_on=FIXED_DATE,
        filters=FilterOptions(dedup=True, near_duplicates=True, thin_content=True),
        **overrides,
    )
    return generate(SITE, parse_screaming_frog_csv(sf_csv), options)


def test_generation_returns_a_non_empty_file(sf_csv: str) -> None:
    """Regression pin for the defect that made the source produce nothing at all.

    It called `datetime.now(datetime.timezone.utc)` while importing only the
    `datetime` class, so every generation raised AttributeError inside the renderer.
    """
    result, _ = _run(sf_csv)

    assert result.llmstxt
    assert result.pages_included > 0


def test_output_is_spec_compliant(sf_csv: str) -> None:
    result, _ = _run(sf_csv)
    lines = result.llmstxt.splitlines()

    assert lines[0] == "# Example", "H1 is the only required element"
    assert any(line.startswith("> ") for line in lines[:5]), "blockquote is required"
    assert "## " in result.llmstxt

    errors = [i for i in result.issues if i.level == "error"]
    assert errors == [], f"spec errors: {[i.message for i in errors]}"


def test_every_link_line_matches_the_spec_shape(sf_csv: str) -> None:
    import re

    result, _ = _run(sf_csv)
    link_lines = [ln for ln in result.llmstxt.splitlines() if ln.startswith("- ")]

    assert link_lines
    for line in link_lines:
        assert re.match(r"^- \[.+\]\(https://.+\): .+$", line), line


def test_optional_section_is_populated(sf_csv: str) -> None:
    """Deep, weakly-linked pages: the paginated tag archives and the 2019 archive."""
    result, _ = _run(sf_csv)

    assert "## Optional" in result.llmstxt
    assert result.optional
    assert any("/legal/subprocessors/archive/2019" in p.url for p in result.optional)


def test_contact_section_is_pinned_before_optional(sf_csv: str) -> None:
    result, _ = _run(sf_csv)
    body = result.llmstxt

    assert "## Contact" in body
    assert body.index("## Contact") < body.index("## Optional")


def test_filters_report_what_they_removed(sf_csv: str) -> None:
    """The source shrank the page set silently; the UI needs the counts."""
    _, reports = _run(sf_csv)
    by_stage = {r.stage: r.count for r in reports}

    assert by_stage["duplicates"] >= 1
    assert by_stage["near duplicates"] == 1
    assert by_stage["thin content"] == 1


def test_titles_lose_the_brand_suffix(sf_csv: str) -> None:
    result, _ = _run(sf_csv)

    assert "Quick Start Guide" in result.llmstxt
    assert "Quick Start Guide | Example" not in result.llmstxt


def test_rebuild_preserves_sections_and_descriptions(sf_csv: str) -> None:
    """The source re-derived sections from URL paths on every edit, so unchecking
    one page collapsed the semantic grouping and dropped section descriptions."""
    result, _ = _run(sf_csv)
    result.sections[0].description = "Hand-written section description."
    original_names = [s.name for s in result.sections]

    edited = rebuild(
        result, excluded_urls={"https://example.com/blog/scaling-ci"}, generated_on=FIXED_DATE
    )

    assert [s.name for s in edited.sections] == original_names
    assert edited.sections[0].description == "Hand-written section description."
    assert "scaling-ci" not in edited.llmstxt


def test_rebuild_also_removes_excluded_pages_from_llms_full(sf_csv: str) -> None:
    """The source rebuilt only llms.txt, so excluded pages still shipped in the
    full and combined downloads."""
    entries = parse_screaming_frog_csv(sf_csv)
    for entry in entries:
        entry.markdown = f"Body text for {entry.url}"

    result, _ = generate(
        SITE,
        entries,
        GenerateOptions(site_name="Example", generated_on=FIXED_DATE, generate_full=True),
    )
    assert "scaling-ci" in result.llms_full

    edited = rebuild(result, {"https://example.com/blog/scaling-ci"}, generated_on=FIXED_DATE)
    assert "scaling-ci" not in edited.llms_full


def test_llms_full_respects_its_character_budget(sf_csv: str) -> None:
    entries = parse_screaming_frog_csv(sf_csv)
    for entry in entries:
        entry.markdown = "x" * 5_000

    result, _ = generate(
        SITE,
        entries,
        GenerateOptions(
            site_name="Example",
            generated_on=FIXED_DATE,
            generate_full=True,
            full_max_chars=12_000,
        ),
    )

    assert len(result.llms_full) <= 12_500, "budget plus the truncation note"
    assert "omitted to keep this file under" in result.llms_full


def test_validator_flags_a_missing_blockquote() -> None:
    issues = validate("# Title\n\n## Docs\n\n- [A](https://e.com/a): B\n", [])
    codes = {i.code for i in issues if i.level == "error"}

    assert "blockquote" in codes


def test_validator_flags_relative_urls() -> None:
    body = "# T\n\n> S\n\n## Docs\n\n- [A](/relative): B\n"
    codes = {i.code for i in validate(body, []) if i.level == "error"}

    assert "relative-url" in codes


def test_golden_output_is_stable(sf_csv: str) -> None:
    """Byte-for-byte pin. Regenerate deliberately with UPDATE_GOLDEN=1."""
    import os

    result, _ = _run(sf_csv)

    if os.environ.get("UPDATE_GOLDEN") == "1":
        GOLDEN.write_text(result.llmstxt, encoding="utf-8", newline="")

    assert GOLDEN.exists(), "run once with UPDATE_GOLDEN=1 to create the golden file"
    assert result.llmstxt == GOLDEN.read_text(encoding="utf-8")
