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

import hashlib
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from typing import Literal
from urllib.parse import urlparse

__all__ = [
    "QUESTIONS",
    "Fact",
    "Question",
    "SiteBrief",
    "brief_from_answers",
    "fingerprint",
    "has_drifted",
    "matches_any",
]

FieldKind = Literal["globs", "urls", "text", "facts"]


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


QUESTIONS: tuple[Question, ...] = (
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

    found_for: str = ""
    audience: str = ""
    valuable: tuple[str, ...] = ()
    noise: tuple[str, ...] = ()
    must_appear: frozenset[str] = frozenset()
    embargoed: tuple[str, ...] = ()
    facts: dict[str, Fact] = field(default_factory=dict)
    # What the site looked like when these answers were given, so a replatform
    # does not run silently on a brief describing the old shape.
    fingerprint: str = ""
    answered_by: str = ""

    @property
    def is_empty(self) -> bool:
        return not any(
            (
                self.found_for,
                self.audience,
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
            "found_for": self.found_for,
            "audience": self.audience,
            "valuable": list(self.valuable),
            "noise": list(self.noise),
            "must_appear": sorted(self.must_appear),
            "embargoed": list(self.embargoed),
            "facts": {
                name: {"value": fact.value, "source": fact.source}
                for name, fact in sorted(self.facts.items())
            },
            "fingerprint": self.fingerprint,
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
            site_fingerprint=str(data.get("fingerprint") or ""),
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
    answers: dict, answered_by: str = "", site_fingerprint: str = ""
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
        found_for=str(answers.get("found_for") or "").strip(),
        audience=str(answers.get("audience") or "").strip(),
        valuable=lines("valuable"),
        noise=lines("noise"),
        must_appear=frozenset(lines("must_appear")),
        embargoed=lines("embargoed"),
        facts=facts,
        fingerprint=site_fingerprint,
        answered_by=answered_by,
    )


# A site gains and loses sitemaps between runs without having changed shape.
# Re-asking on any difference at all would make "once per domain" meaningless.
DRIFT_URL_RATIO = 0.20


def fingerprint(group_names: list[str], url_count: int) -> str:
    """A stable summary of site shape, cheap to store and compare."""
    digest = hashlib.sha256("\n".join(sorted(set(group_names))).encode()).hexdigest()[:16]
    return f"{digest}:{url_count}"


def has_drifted(previous: str, group_names: list[str], url_count: int) -> str | None:
    """Say whether the answers should be re-confirmed, and why.

    Returns the reason, so the UI can tell the operator what changed rather than
    presenting the same form again with no explanation.
    """
    if not previous or ":" not in previous:
        return None
    old_digest, _, old_count_text = previous.partition(":")
    try:
        old_count = int(old_count_text)
    except ValueError:
        return None

    new_digest = fingerprint(group_names, url_count).partition(":")[0]
    if new_digest != old_digest:
        return "The site's sitemap groups have changed since these answers were given."
    if old_count and abs(url_count - old_count) / old_count > DRIFT_URL_RATIO:
        direction = "grown" if url_count > old_count else "shrunk"
        return (
            f"The site has {direction} from {old_count:,} to {url_count:,} URLs since these "
            "answers were given."
        )
    return None
