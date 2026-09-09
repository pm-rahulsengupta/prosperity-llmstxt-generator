"""One decision: is the result of this edit worse than what it replaced?

There were two gates and they disagreed about what "worse" means.

`llms.txt` compared the *set of error codes* before and after, so an edit that
turned one relative URL into forty passed -- the code was already there. It also
never looked at warnings, and it read `validate()`, which emits three codes.

`agents.md` compared rule ids **and counts**, treated AGT-004 as absolute rather
than as merely-not-newly-failing, and said why in its own docstring:

    "The llms.txt gate compares error *codes* and lets an edit worsen a failure
    it already had. This compares rule ids and counts, because 'it was already a
    bit broken' is not a reason to let something break it further."

That is the correct one, so it is the only one. Generalising it costs nothing
except naming the invariants per rule set, which had to be written down anyway.

## What this deliberately does not do

It does not decide whether an edit is *good*. A turn that removes a section the
operator wanted is not a regression and this will not catch it -- that is what
the diff preview is for. This answers one narrower question, and answering it
well is what lets the preview be advisory rather than load-bearing.

It also does not gate on warnings or infos. An edit that adds a warning is
usually the operator doing something deliberate, and a gate that blocks those
teaches people to stop asking for legitimate changes -- which costs more than
the warnings do.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["ABSOLUTE", "Verdict", "check"]

#: Rules that must not fail *at all* after an edit, even where they already were.
#: An absolute is not a strict rule; it is a rule about a claim that must never
#: appear in a published file, so "it was already there" is not a defence.
#:
#: AGT-004 is the whole reason `refine` has no operation that adds a URL: a file
#: naming an endpoint no probe confirmed is a file that sends an agent somewhere
#: we invented. XF-001 is the cross-file one -- an `llms-full.txt` with no
#: blockquote shipped for the life of this tool because nothing read it.
ABSOLUTE: dict[str, tuple[str, ...]] = {
    "agents": ("AGT-004",),
    "index": (),
    "full": (),
    "markdown": ("MD-006",),
    "okf": ("OKF-003",),
    "info": ("INF-002", "INF-003"),
    "crawl": (),
}


@dataclass(slots=True)
class Verdict:
    """Whether the edit may be kept, and what it would have broken.

    `reasons` is written for an operator rather than for a log. A refusal that
    says only "rejected" teaches someone to stop asking; one that names the rule
    and the direction lets them ask for something else.
    """

    reasons: list[str] = field(default_factory=list)

    @property
    def kept(self) -> bool:
        return not self.reasons

    def __bool__(self) -> bool:  # `if verdict:` reads as "is it allowed"
        return self.kept


def _errors(report, rules_by_id) -> dict[str, object]:
    """Error-severity failures, keyed by rule id.

    Severity is read from the rule rather than the finding, because a finding
    carries the outcome and the rule carries the weight -- and a report from a
    different rule set would otherwise be silently accepted here.
    """
    from app.core.rules.registry import Severity

    out: dict[str, object] = {}
    for finding in getattr(report, "failures", []):
        rule = rules_by_id.get(finding.rule_id)
        if rule is not None and rule.severity is Severity.ERROR:
            out[finding.rule_id] = finding
    return out


def check(before, after, *, judged_by: str, rules_by_id) -> Verdict:
    """Compare two reports over the same file and say whether to keep the second.

    Three tests, in the order they matter:

    1. **An absolute failed.** Not "newly failed" -- failed. See `ABSOLUTE`.
    2. **A rule that was passing is now failing.**
    3. **A rule that was failing now fails harder.** This is the one the llms.txt
       gate lacked, and it is the one that matters most on a bulk edit: rewriting
       every description at once is exactly the shape of turn that takes one
       finding to forty without adding a code.

    `before` may be `None`, which means the file did not exist until this edit.
    Everything in `after` is then new, so only the absolutes apply -- judging a
    first draft against a predecessor it has not got would refuse every one.
    """
    verdict = Verdict()

    for rule_id in ABSOLUTE.get(judged_by, ()):
        # `Report.failed` already folds "no such finding" and "did not fail" into
        # one answer, which is the answer this wants: a rule that did not run is
        # not a rule that failed, and it is also not a reason to refuse an edit.
        if after.failed(rule_id):
            verdict.reasons.append(f"{rule_id}: {after.by_id(rule_id).message}")

    if before is None:
        return verdict

    was = _errors(before, rules_by_id)
    now = _errors(after, rules_by_id)

    for rule_id, finding in now.items():
        if rule_id not in was:
            verdict.reasons.append(f"{rule_id} would start failing")
        elif getattr(finding, "count", 1) > getattr(was[rule_id], "count", 1):
            verdict.reasons.append(
                f"{rule_id} would go from {was[rule_id].count} to {finding.count}"
            )

    return verdict
