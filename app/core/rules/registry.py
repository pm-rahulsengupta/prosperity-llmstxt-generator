"""Rule definitions, findings, and the score.

Three things here are load-bearing and easy to get wrong, so they are stated plainly.

**A rule that did not run is not a rule that passed.** Network rules offline, full-file
rules with no full file, profile rules on a third-party file whose profile is unknown —
all of these are `SKIPPED`, excluded from the denominator, and listed. Counting them as
passes inflates every score, and the inflation is largest exactly when we know least.
An offline 100 and a networked 100 are different claims and the report says which.

**Findings aggregate per rule.** The reference bad file has 106 banned openers. That is
one finding with a count and a few examples, not 106 issues. Otherwise the report is
unreadable and the score becomes a function of file length rather than file quality.

**Severity is the weight.** An error costs more than a warning, which costs more than
info. The score is the share of applicable weight that passed, so a file failing one
error scores worse than a file failing three infos — which is the ordering anyone
reading the number expects.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class Category(StrEnum):
    INDEX = "IDX"
    FULL = "FULL"
    CROSS = "XF"


class Outcome(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    SKIPPED = "skipped"


# What each severity contributes to the denominator. The gap between error and warning
# is deliberately wide: a file that is not valid markdown-structured llms.txt is not
# "mostly fine with a few notes".
SEVERITY_WEIGHT: dict[Severity, float] = {
    Severity.ERROR: 5.0,
    Severity.WARNING: 2.0,
    Severity.INFO: 1.0,
}


@dataclass(slots=True)
class Finding:
    """One rule's verdict on one document."""

    rule_id: str
    outcome: Outcome
    message: str = ""
    # How many times the rule's condition was hit -- 106 banned openers, not 106 findings.
    count: int = 0
    # A few concrete instances, for the report. Never the full list.
    examples: list[str] = field(default_factory=list)
    # Why a rule was skipped. Required for SKIPPED, so a skip is never unexplained.
    reason: str = ""

    @property
    def failed(self) -> bool:
        return self.outcome is Outcome.FAIL


@dataclass(slots=True)
class Rule:
    id: str
    title: str
    category: Category
    severity: Severity
    check: Callable[[RuleContext], Finding]
    # Why the rule exists, in one line. Shown in the report, because a rule nobody
    # understands is a rule people disable.
    rationale: str = ""

    def run(self, ctx: RuleContext) -> Finding:
        try:
            return self.check(ctx)
        except Exception as exc:  # a broken rule must not take the report down
            return Finding(
                rule_id=self.id,
                outcome=Outcome.SKIPPED,
                reason=f"rule raised {type(exc).__name__}: {exc}",
            )


@dataclass(slots=True)
class ProfileBounds:
    """The profile-dependent limits. Absent for a third-party file."""

    name: str = ""
    max_bytes: int = 0
    min_links: int = 0
    max_links: int = 0
    sections: list[str] = field(default_factory=list)
    section_min: dict[str, int] = field(default_factory=dict)
    section_max: dict[str, int] = field(default_factory=dict)
    identity_patterns: list[str] = field(default_factory=list)
    banned_openers: list[str] = field(default_factory=list)
    banned_superlatives: list[str] = field(default_factory=list)
    locale: str = ""

    @property
    def known(self) -> bool:
        return bool(self.name)


@dataclass(slots=True)
class RuleContext:
    """Everything the rules may look at."""

    index: object | None = None  # IndexDoc
    full: object | None = None  # FullDoc
    profile: ProfileBounds = field(default_factory=ProfileBounds)
    # Populated by the worker; empty means the network rules did not run.
    link_status: dict[str, int | str] = field(default_factory=dict)
    network_checked: bool = False
    # Canonical facts, for the entity rules. Empty means unknown, never "no conflict".
    facts: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class Report:
    findings: list[Finding] = field(default_factory=list)
    score: int = 0
    applicable_weight: float = 0.0
    passed_weight: float = 0.0
    # Set when a severity cap held the score down, so the number is explicable.
    capped_by: str = ""

    @property
    def failures(self) -> list[Finding]:
        return [f for f in self.findings if f.outcome is Outcome.FAIL]

    @property
    def skipped(self) -> list[Finding]:
        return [f for f in self.findings if f.outcome is Outcome.SKIPPED]

    def by_id(self, rule_id: str) -> Finding | None:
        return next((f for f in self.findings if f.rule_id == rule_id), None)

    def failed(self, rule_id: str) -> bool:
        finding = self.by_id(rule_id)
        return finding is not None and finding.failed


def score_report(findings: Iterable[Finding], rules: dict[str, Rule]) -> Report:
    """Share of applicable severity weight that passed, as 0-100.

    Skipped rules are excluded from both numerator and denominator. A document where
    everything was skipped scores 0 with an empty denominator rather than a misleading
    100 -- "nothing was checked" must not read as "nothing was wrong".
    """
    report = Report(findings=list(findings))

    for finding in report.findings:
        rule = rules.get(finding.rule_id)
        if rule is None or finding.outcome is Outcome.SKIPPED:
            continue
        weight = SEVERITY_WEIGHT[rule.severity]
        report.applicable_weight += weight
        if finding.outcome is Outcome.PASS:
            report.passed_weight += weight

    raw = (
        round(100 * report.passed_weight / report.applicable_weight)
        if report.applicable_weight
        else 0
    )

    # Severity caps. Without them a nearly-empty file scores well by passing a pile
    # of rules vacuously -- no duplicate links, no superlatives, no stray HTML -- while
    # failing the one rule that says it is a valid llms.txt at all. A file missing its
    # H1 is not "82/100 with a note"; the bands have to mean something:
    #
    #   90-100  publishable        80-89  minor issues only
    #   50-79   a warning failed   0-49   an error failed
    failed_severities = {rules[f.rule_id].severity for f in report.failures if f.rule_id in rules}
    if Severity.ERROR in failed_severities:
        raw = min(raw, 49)
        report.capped_by = "error"
    elif Severity.WARNING in failed_severities:
        raw = min(raw, 79)
        report.capped_by = "warning"

    report.score = raw
    return report


def ok(rule_id: str, message: str = "") -> Finding:
    return Finding(rule_id=rule_id, outcome=Outcome.PASS, message=message)


def fail(rule_id: str, message: str, count: int = 1, examples: Iterable[str] = ()) -> Finding:
    return Finding(
        rule_id=rule_id,
        outcome=Outcome.FAIL,
        message=message,
        count=count,
        examples=[str(e)[:160] for e in list(examples)[:5]],
    )


def skip(rule_id: str, reason: str) -> Finding:
    return Finding(rule_id=rule_id, outcome=Outcome.SKIPPED, reason=reason)
