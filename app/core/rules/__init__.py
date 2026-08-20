"""The audit engine: parse, run every applicable rule, score.

Public surface is `audit()`. Everything else is a rule.

Cross-file rules live here rather than in their own module because there are only
two that are decidable without a model, and both are one comparison. The rest of the
brief's XF set — "zero entity-fact contradictions" — is an extraction-and-
reconciliation problem, not a rule-engine one. A scored rule whose verdict depends on
a model is not diffable over time, which is the whole reason the rule IDs exist, so
that work belongs in the QA stage and an unscored advisory list.
"""

from __future__ import annotations

from app.core.rules.document import FullDoc, IndexDoc, parse_full, parse_index
from app.core.rules.full_rules import FULL_RULES
from app.core.rules.index_rules import INDEX_RULES
from app.core.rules.registry import (
    Category,
    Finding,
    Outcome,
    ProfileBounds,
    Report,
    Rule,
    RuleContext,
    Severity,
    fail,
    ok,
    score_report,
    skip,
)


def xf_001(ctx):
    """The blockquote must match across the two files.

    They are the same claim about the same organisation. Two different ones means one
    was regenerated and the other was not, and a reader has no way to tell which is
    current.
    """
    if ctx.index is None or ctx.full is None:
        return skip("XF-001", "needs both files")
    index_quote = ctx.index.blockquote.strip()
    full_quote = ctx.full.blockquote.strip()
    if not full_quote:
        return fail("XF-001", "The full file has no blockquote summary.")
    if index_quote and index_quote != full_quote:
        return fail(
            "XF-001",
            "The blockquote differs between the index and the full file.",
            examples=[f"index: {index_quote[:80]}", f"full: {full_quote[:80]}"],
        )
    return ok("XF-001")


def xf_002(ctx):
    """Every indexed URL should appear in the full file, or the gap should be known."""
    if ctx.index is None or ctx.full is None:
        return skip("XF-002", "needs both files")
    indexed = {link.url.rstrip("/") for link in ctx.index.links if link.url}
    present = {page.source.rstrip("/") for page in ctx.full.pages if page.source}
    missing = sorted(indexed - present)
    if not missing:
        return ok("XF-002")
    return fail(
        "XF-002",
        f"{len(missing)} of {len(indexed)} indexed URL(s) are absent from the full file. "
        "A reader given only the full file cannot reach them.",
        count=len(missing),
        examples=missing,
    )


CROSS_RULES: list[Rule] = [
    Rule(
        "XF-001",
        "Blockquote matches across files",
        Category.CROSS,
        Severity.WARNING,
        xf_001,
        "Two different summaries means one is stale.",
    ),
    Rule(
        "XF-002",
        "Indexed URLs present in the full file",
        Category.CROSS,
        Severity.WARNING,
        xf_002,
        "The full file is supposed to contain what the index points at.",
    ),
]

ALL_RULES: list[Rule] = [*INDEX_RULES, *FULL_RULES, *CROSS_RULES]
RULES_BY_ID: dict[str, Rule] = {rule.id: rule for rule in ALL_RULES}


def audit(
    index_text: str = "",
    full_text: str = "",
    *,
    profile: ProfileBounds | None = None,
    link_status: dict[str, int | str] | None = None,
    network_checked: bool = False,
) -> Report:
    """Run every rule that applies and score the result.

    Pure: no network, no database. Link statuses are passed in by whoever did the
    fetching, exactly as `issues_from_link_check` already works, so the rule engine
    stays callable from a request without blocking it.
    """
    ctx = RuleContext(
        index=parse_index(index_text) if index_text.strip() else None,
        full=parse_full(full_text) if full_text.strip() else None,
        profile=profile or ProfileBounds(),
        link_status=link_status or {},
        network_checked=network_checked,
    )

    findings = []
    for rule in ALL_RULES:
        if rule.category is Category.INDEX and ctx.index is None:
            findings.append(skip(rule.id, "no llms.txt supplied"))
            continue
        findings.append(rule.run(ctx))

    return score_report(findings, RULES_BY_ID)


def render_text(report: Report, *, verbose: bool = False) -> str:
    """A human-readable report. The score is never shown without its coverage."""
    total = len(report.findings)
    skipped = len(report.skipped)
    checked = total - skipped

    lines = [
        f"Score: {report.score}/100   ({checked} of {total} rules ran"
        + (f"; {skipped} could not be checked" if skipped else "")
        + ")",
        "",
    ]

    order = {Severity.ERROR: 0, Severity.WARNING: 1, Severity.INFO: 2}
    failures = sorted(report.failures, key=lambda f: order[RULES_BY_ID[f.rule_id].severity])
    for finding in failures:
        rule = RULES_BY_ID[finding.rule_id]
        lines.append(f"  {rule.severity.value.upper():<8} {finding.rule_id}  {finding.message}")
        for example in finding.examples:
            lines.append(f"           - {example}")

    if report.skipped:
        lines += ["", "Not checked:"]
        lines += [f"  {f.rule_id}  {f.reason}" for f in report.skipped]

    if verbose:
        passes = [f for f in report.findings if f.outcome is Outcome.PASS]
        lines += ["", f"Passed: {', '.join(f.rule_id for f in passes)}"]

    return "\n".join(lines)


__all__ = [
    "ALL_RULES",
    "RULES_BY_ID",
    "Finding",
    "FullDoc",
    "IndexDoc",
    "Outcome",
    "ProfileBounds",
    "Report",
    "Rule",
    "Severity",
    "audit",
    "parse_full",
    "parse_index",
    "render_text",
]
