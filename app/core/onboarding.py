"""What the operator knows before the crawl starts.

Metrics tell us what a site earned. They cannot tell us what it is *for*: a
services page that has never ranked is still the page a buyer needs, and a
retired campaign directory can carry three years of decaying traffic. The
onboarding brief is where a person supplies that, once per domain, in a form
the code can act on rather than a form only a prompt can read.

Two rules shape the whole module.

**Declared value is a floor, not a ceiling.** Saying a pattern matters stops it
being excluded on traffic alone; it does not force it in. A declared group whose
data is genuinely empty lands in ``REVIEW`` and shows up at the review gate,
which is the honest outcome -- the alternative admits four thousand facet URLs
because somebody typed one careless glob. The exception is ``embargoed``, which
is absolute: legal restrictions are not evidence to be weighed.

**Free text never gets a deterministic effect.** ``audience`` and ``found_for``
reach the plan and summarise prompts and nothing else. Our own summarise prompt
asking nicely for good openers, and getting 106 banned ones, is the standing
argument against pretending prompt text is an enforcement mechanism.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from fnmatch import fnmatchcase
from typing import Literal
from urllib.parse import urlparse

__all__ = [
    "ACTION_LABELS",
    "QUESTIONS",
    "Drift",
    "Fact",
    "PrimaryAction",
    "Question",
    "SiteBrief",
    "brief_from_answers",
    "detect_drift",
    "fold_change",
    "matches_any",
    "site_shape",
    "split_embargoed",
]

# `text` answers reach a model and nothing else. `published` answers are written
# verbatim into a generated file, which makes them deterministic despite being
# prose -- a distinction worth a separate kind, because the rule that free text
# has no automatic effect is real and this would quietly break it.
FieldKind = Literal["globs", "urls", "text", "published", "choice", "facts"]


class PrimaryAction(StrEnum):
    """What the operator most wants an agent to do on this site.

    The single most useful answer in the brief, because it is the one thing no
    amount of crawling reveals. Two sites can be structurally identical and want
    opposite things from an agent: a clinic wants a booking, a manufacturer with
    the same page shapes wants a distributor enquiry. Everything downstream --
    which agents.md profile is written, whether commerce endpoints are offered at
    all, which resources are worth showing -- follows from this.

    Deliberately actions rather than industries. "Legal services" says what a firm
    is; "have an agent make contact" says what it wants, and only the second can
    be written into an instruction file.
    """

    CONTACT_LOCAL = "contact_local_business"
    CONTACT_AGENCY = "contact_agency"
    BOOK_APPOINTMENT = "book_appointment"
    SHOP_ON_STORE = "shop_on_store"
    FIND_LOCAL_INVENTORY = "find_local_inventory"
    READ_AND_CITE = "read_and_cite"
    USE_THE_API = "use_the_api"
    UNDECIDED = ""


ACTION_LABELS: dict[PrimaryAction, str] = {
    PrimaryAction.CONTACT_LOCAL: "Contact a local business (call, directions, opening hours)",
    PrimaryAction.CONTACT_AGENCY: "Make an enquiry with an agency or firm",
    PrimaryAction.BOOK_APPOINTMENT: "Book an appointment or consultation",
    PrimaryAction.SHOP_ON_STORE: "Buy something on the store",
    PrimaryAction.FIND_LOCAL_INVENTORY: "Find stock in a nearby location, then buy or reserve",
    PrimaryAction.READ_AND_CITE: "Read and cite the content accurately",
    PrimaryAction.USE_THE_API: "Use the API or documentation",
}

# The actions that involve money changing hands. This, not the platform, is what
# opens the commerce sections of an agents.md -- a WooCommerce install on a site
# whose real goal is enquiries should not be handed a checkout flow, and the
# operator is the only one who can say which it is.
TRANSACTIONAL_ACTIONS = frozenset({PrimaryAction.SHOP_ON_STORE, PrimaryAction.FIND_LOCAL_INVENTORY})


@dataclass(frozen=True, slots=True)
class Question:
    """One onboarding question, as data, so the form and the prompt agree.

    The UI renders these, the plan prompt reads the answers to the ``text`` ones,
    and the pipeline acts on the rest. Adding a question is one entry.
    """

    key: str
    prompt: str
    kind: FieldKind
    # Shown under the field. Says what the answer will actually do, because an
    # operator cannot calibrate an answer without knowing its consequence.
    effect: str
    placeholder: str = ""
    # (value, label) pairs for `choice` questions. Empty for every other kind.
    choices: tuple[tuple[str, str], ...] = ()


QUESTIONS: tuple[Question, ...] = (
    Question(
        key="primary_action",
        prompt="What do you most want an AI agent to do on this site?",
        kind="choice",
        effect=(
            "Chooses which agents.md is written and whether commerce endpoints are "
            "offered at all. The one answer no amount of crawling can supply."
        ),
        placeholder="",
        choices=tuple(ACTION_LABELS.items()),
    ),
    Question(
        key="found_for",
        prompt="What should this site be found for?",
        kind="text",
        effect="Given to the planning and description stages as context. No automatic effect.",
        placeholder="Digital PR and link building for Australian brands",
    ),
    Question(
        key="audience",
        prompt="Who reads the answers a model gives about this site?",
        kind="text",
        effect="Steers profile selection and the voice of link descriptions. No automatic effect.",
        placeholder="Marketing managers evaluating agencies; not developers",
    ),
    Question(
        key="valuable",
        prompt="Which URL patterns are the most valuable?",
        kind="globs",
        effect=(
            "A floor: these are never excluded on traffic alone. Worst case they are held "
            "for review. They are not forced in."
        ),
        placeholder="/services/*\n/case-studies/*\nAllUsed_Make",
    ),
    Question(
        key="noise",
        prompt="Which patterns do you already know are noise?",
        kind="globs",
        effect=(
            "A ceiling: never included wholesale. Where such a pattern still holds a page "
            "earning real clicks, that page is kept and the tail dropped."
        ),
        placeholder="/tag/*\n/author/*\n/?s=*",
    ),
    Question(
        key="must_appear",
        prompt="Which pages must appear regardless of what the numbers say?",
        kind="urls",
        effect="Absolute. Joins the identity set, which no traffic rule can exclude.",
        placeholder="https://example.com/about/",
    ),
    Question(
        key="embargoed",
        prompt="Is anything under embargo, legal restriction, or NDA?",
        kind="globs",
        effect="Absolute exclusion. No evidence overrides it and it is never sent to a model.",
        placeholder="/clients/acquisition-2026/*",
    ),
    Question(
        key="rate_limit_note",
        prompt="What request rate should automated agents keep to?",
        kind="published",
        effect=(
            "Published in agents.md as guidance for agents. Left out entirely when "
            "blank -- we do not invent a limit a client never agreed to advertise."
        ),
        placeholder="One request per second; identify yourself in the User-Agent",
    ),
    Question(
        key="facts",
        prompt="Facts the tool must never guess: founding year, locations, team size, awards.",
        kind="facts",
        effect=(
            "Checked against the corpus by the consistency audit. A fact that is missing "
            "becomes a blocking question rather than an invention."
        ),
        placeholder="founded = 2013",
    ),
)


@dataclass(frozen=True, slots=True)
class Fact:
    """A canonical claim, with where it came from.

    A fact with no source cannot be defended to a client when the audit reports
    that their About page contradicts it, so ``source`` is not optional.
    """

    value: str
    source: str


@dataclass(frozen=True, slots=True)
class SiteBrief:
    """The answers, structured. Persisted per domain under ``site_configs.plan``."""

    primary_action: PrimaryAction = PrimaryAction.UNDECIDED
    found_for: str = ""
    audience: str = ""
    # Advertised in agents.md. Free text rather than a number: "one request per
    # second, identify yourself" is the useful form, and a bare integer would need
    # a unit, a scope and a burst allowance to mean the same thing.
    rate_limit_note: str = ""
    valuable: tuple[str, ...] = ()
    noise: tuple[str, ...] = ()
    must_appear: frozenset[str] = frozenset()
    embargoed: tuple[str, ...] = ()
    facts: dict[str, Fact] = field(default_factory=dict)
    # URL count per sitemap group when these answers were given, so a replatform
    # does not run silently on a brief describing the old shape -- and so drift
    # can name which groups moved rather than only that something did.
    shape: dict[str, int] = field(default_factory=dict)
    answered_by: str = ""

    @property
    def is_empty(self) -> bool:
        return not any(
            (
                self.primary_action,
                self.found_for,
                self.audience,
                self.rate_limit_note,
                self.valuable,
                self.noise,
                self.must_appear,
                self.embargoed,
                self.facts,
            )
        )

    def to_dict(self) -> dict:
        """JSONB-safe. Sets and tuples become lists; Fact becomes a mapping."""
        return {
            "primary_action": self.primary_action.value,
            "found_for": self.found_for,
            "audience": self.audience,
            "rate_limit_note": self.rate_limit_note,
            "valuable": list(self.valuable),
            "noise": list(self.noise),
            "must_appear": sorted(self.must_appear),
            "embargoed": list(self.embargoed),
            "facts": {
                name: {"value": fact.value, "source": fact.source}
                for name, fact in sorted(self.facts.items())
            },
            "shape": dict(self.shape),
            "answered_by": self.answered_by,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> SiteBrief:
        """Tolerant by design: an older stored brief must still load.

        `brief_from_answers` already normalises every field this reads, so a
        brief written before a question existed loads with that field empty
        rather than raising on a schema that has since moved.
        """
        if not data:
            return cls()
        brief = brief_from_answers(
            data,
            answered_by=str(data.get("answered_by") or ""),
            shape=dict(data.get("shape") or {}),
        )
        return brief

    def prompt_context(self) -> str:
        """The part a model is allowed to see.

        Deliberately excludes ``embargoed``: naming a restricted path to an LLM
        is the disclosure that answer existed to prevent.
        """
        lines: list[str] = []
        if self.found_for:
            lines.append(f"The operator wants this site found for: {self.found_for}")
        if self.audience:
            lines.append(f"The audience is: {self.audience}")
        if self.valuable:
            lines.append(
                "Patterns the operator considers most valuable: " + ", ".join(self.valuable)
            )
        if self.noise:
            lines.append("Patterns the operator considers low value: " + ", ".join(self.noise))
        for name, fact in sorted(self.facts.items()):
            lines.append(f"Established fact, {name}: {fact.value} (source: {fact.source})")
        return "\n".join(lines)


def fold_change(before: float, after: float) -> float:
    """How many times larger the bigger of two magnitudes is. Always >= 1.0.

    The house measure for comparing two magnitudes, in place of percentage
    change. A percentage is asymmetric: a quantity can grow without limit but can
    only ever fall by 100%, so any threshold at or above 1.0 catches doubling and
    can never catch halving. Drift shipped with exactly that bug -- a sitemap
    group gutted from 4,000 URLs to 200 is a 95% loss that no percentage
    threshold in the usable range would fire on, while the same group doubling
    tripped immediately.

    Use this for every before-and-after comparison: click trends, CTR movement,
    coverage between runs, group sizes. Do *not* use it for shares of a whole --
    coverage, orphan share, CTR itself -- which are bounded fractions rather than
    two magnitudes being compared, and for which a percentage is the right unit.

    It lives in this module rather than in `metrics`, which is where trends will
    be written, only because `metrics` imports this one and the dependency
    cannot run both ways. `metrics` re-exports it, so callers there need not know.

    Returns `inf` when something appears from nothing, which is a real event and
    not a division error; 1.0 when both are zero, since nothing changed.
    """
    lo, hi = sorted((abs(before), abs(after)))
    if lo == 0:
        return 1.0 if hi == 0 else float("inf")
    return hi / lo


def _as_action(raw) -> PrimaryAction:
    """Tolerant: an unknown or absent answer is undecided, never a guess."""
    try:
        return PrimaryAction(str(raw or "").strip())
    except ValueError:
        return PrimaryAction.UNDECIDED


def _normalise(pattern: str) -> str:
    """Accept what a person types.

    ``fnmatch``'s ``*`` already crosses ``/``, so ``/guides/*`` matches
    ``/guides/a/b``. Operators write ``**`` anyway, out of habit from gitignore
    and shell globs, where it would otherwise be a literal pair of asterisks
    matching nothing.
    """
    return pattern.strip().replace("**", "*")


def matches_any(candidate: str, patterns: tuple[str, ...]) -> str | None:
    """Return the pattern that matched, or None.

    Returns the pattern rather than a bool so a verdict can name the answer that
    produced it. "Held for review because you declared /services/*" is auditable;
    "held for review" is not.

    A candidate is tried as a full URL, as its path, and as a bare group name,
    because an operator will type ``/services/*`` for one and ``AllNew_*`` for
    the other and should not have to know which the code is holding.
    """
    if not patterns:
        return None
    forms = {candidate}
    parsed = urlparse(candidate)
    if parsed.scheme and parsed.path:
        forms.add(parsed.path)
        forms.add(parsed.path.rstrip("/"))
        forms.add(parsed.path.rstrip("/") + "/")
    for pattern in patterns:
        rule = _normalise(pattern)
        if not rule:
            continue
        for form in forms:
            if fnmatchcase(form, rule) or fnmatchcase(form.lower(), rule.lower()):
                return pattern
    return None


def brief_from_answers(
    answers: dict, answered_by: str = "", shape: dict[str, int] | None = None
) -> SiteBrief:
    """Build a brief from raw form input, tolerating the shapes a form produces."""

    def lines(key: str) -> tuple[str, ...]:
        raw = answers.get(key) or ()
        if isinstance(raw, str):
            raw = raw.splitlines()
        return tuple(dict.fromkeys(item.strip() for item in raw if item and item.strip()))

    facts: dict[str, Fact] = {}
    for name, value in (answers.get("facts") or {}).items():
        if isinstance(value, Fact):
            facts[name] = value
        elif isinstance(value, dict):
            facts[name] = Fact(str(value.get("value", "")), str(value.get("source", "")))
        else:
            # A bare value with no provenance is still a fact somebody typed;
            # record who, rather than dropping it or inventing a citation.
            facts[name] = Fact(str(value), "operator")

    return SiteBrief(
        primary_action=_as_action(answers.get("primary_action")),
        found_for=str(answers.get("found_for") or "").strip(),
        audience=str(answers.get("audience") or "").strip(),
        rate_limit_note=str(answers.get("rate_limit_note") or "").strip(),
        valuable=lines("valuable"),
        noise=lines("noise"),
        must_appear=frozenset(lines("must_appear")),
        embargoed=lines("embargoed"),
        facts=facts,
        shape=site_shape(shape or {}),
        answered_by=answered_by,
    )


# Drift detection.
#
# The first version summed URL counts across the whole site and re-asked on a 20%
# swing. That is wrong in both directions on a real property. Gumtree's listing
# count moves more than 20% on ordinary churn, so it would nag constantly; and a
# group disappearing while another doubles nets to roughly zero, so the change
# that actually matters -- the site being restructured -- would pass silently.
#
# Group names carry the signal. A group appearing or disappearing means the site
# has been reorganised and the answers may no longer describe it, so tolerance
# there is zero. Counts are noisy by nature and belong per-group behind a wide
# band, where they catch a section being gutted or exploding rather than a
# fortnight of publishing.
# Fold-change, not percentage change. A percentage is asymmetric: a group can
# grow without limit but can only ever shrink by 100%, so any threshold at or
# above 1.0 makes shrinking undetectable -- a section gutted from 4,000 URLs to
# 200 is a 95% loss and would never have fired. Fold-change treats halving and
# doubling as the same size of event, which is what they are.
DRIFT_COUNT_FOLD = 2.0
DRIFT_COUNT_FLOOR = 50


@dataclass(frozen=True, slots=True)
class Drift:
    """What changed about a site's shape since the brief was answered.

    Carries the affected group names, not just a boolean. The action on drift is
    to re-approve *the groups that moved* -- invalidating the whole plan would
    discard every human decision on the groups that did not, which is the state
    the storage rules exist to protect.
    """

    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    resized: tuple[tuple[str, int, int], ...] = ()

    @property
    def drifted(self) -> bool:
        return bool(self.added or self.removed or self.resized)

    @property
    def affected(self) -> frozenset[str]:
        """Exactly the groups needing another look. Everything else stands."""
        return frozenset([*self.added, *self.removed, *(name for name, _, _ in self.resized)])

    def reason(self) -> str:
        """One line for the operator, naming what moved rather than that something did."""
        parts = []
        if self.added:
            parts.append(f"{len(self.added)} new group(s): {', '.join(sorted(self.added)[:4])}")
        if self.removed:
            parts.append(
                f"{len(self.removed)} group(s) gone: {', '.join(sorted(self.removed)[:4])}"
            )
        for name, old, new in self.resized[:3]:
            direction = "grew" if new > old else "shrank"
            parts.append(f"{name} {direction} from {old:,} to {new:,} URLs")
        return "; ".join(parts)

    def to_dict(self) -> dict:
        return {
            "added": list(self.added),
            "removed": list(self.removed),
            "resized": [list(item) for item in self.resized],
        }


def site_shape(group_counts: dict[str, int]) -> dict[str, int]:
    """The stored shape: URL count per sitemap group.

    Per-group rather than a digest plus a total, because a digest cannot say
    *which* group moved and the action on drift needs exactly that.
    """
    return {str(name): int(count) for name, count in group_counts.items()}


def detect_drift(
    previous: dict[str, int] | None,
    current: dict[str, int],
    count_fold: float = DRIFT_COUNT_FOLD,
    count_floor: int = DRIFT_COUNT_FLOOR,
) -> Drift:
    """Compare two site shapes.

    `count_floor` keeps small groups quiet: a group going from 2 URLs to 5 is a
    150% change and means nothing, and without a floor every such group would be
    reported on every run until the operator stopped reading the warnings.
    """
    if not previous:
        return Drift()

    added = tuple(sorted(set(current) - set(previous)))
    removed = tuple(sorted(set(previous) - set(current)))

    resized = []
    for name in sorted(set(previous) & set(current)):
        old, new = previous[name], current[name]
        if max(old, new) < count_floor:
            continue
        if fold_change(old, new) >= count_fold:
            resized.append((name, old, new))

    return Drift(added=added, removed=removed, resized=tuple(resized))


def split_embargoed(urls: list[str], brief: SiteBrief | None) -> tuple[list[str], dict[str, int]]:
    """Partition a crawl list into what may be fetched and what may not.

    Returns the survivors and a count per pattern. The counts exist so the
    suppression can be reported: the patterns are deliberately hidden from the
    model, which means the planner can propose an embargoed group in good faith
    and watch it vanish with no explanation anywhere. Hidden from the model is
    not the same as hidden from the operator, and conflating the two turns "why
    does this page never appear" into a debugging session with no trail.
    """
    if brief is None or not brief.embargoed:
        return urls, {}

    kept: list[str] = []
    counts: dict[str, int] = {}
    for url in urls:
        if (pattern := matches_any(url, brief.embargoed)) is not None:
            counts[pattern] = counts.get(pattern, 0) + 1
        else:
            kept.append(url)
    return kept, counts
