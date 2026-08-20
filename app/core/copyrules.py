"""Deterministic checks on link-line copy, applied after the describe stage.

The argument for this module is one measurement. Our own generated file for
prosperitymedia.com.au contains 106 descriptions opening with Learn, Discover,
Explore or Understand, and 41 unverifiable superlatives — from a pipeline whose
prompt already said, in as many words, that `"Learn more about our services" is a
failure`.

A prompt is guidance, not an enforcement mechanism. The prompt has been fixed too,
but the check is what makes it true.

Everything here is pure and returns verdicts; regeneration and flagging are the
caller's job. `app/llm/stages.py::rewrite_failed_copy` gives a failing line exactly
one more attempt and then flags it rather than shipping it — the same "regenerate
once, then flag" rule the review brief specifies.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Openers that name the reader's activity rather than the page's content. "Learn
# about authentication" tells an agent choosing between forty links nothing at all.
BANNED_OPENERS: tuple[str, ...] = (
    "learn",
    "discover",
    "explore",
    "understand",
    "find out",
    "gain",
    "dive into",
    "unlock",
    "get to know",
    "read about",
    "take a look",
)

# Unverifiable claims. A machine-readable index is not the place for them, and a
# model reading the file cannot check any of them.
BANNED_SUPERLATIVES: tuple[str, ...] = (
    "award-winning",
    "best",
    "leading",
    "top",
    "world-class",
    "dominate",
    "ultimate",
    "premier",
    "cutting-edge",
    "proven",
    "expert",
    "best-in-class",
    "unrivalled",
    "unrivaled",
    "market-leading",
)

# Curated pairs. A general -ise/-ize regex is worthless: it matches "enterprise",
# "expertise", "advise", "size" and "prize". Left value is US, right is AU/GB.
LOCALE_PAIRS: tuple[tuple[str, str], ...] = (
    ("optimize", "optimise"),
    ("optimization", "optimisation"),
    ("analyze", "analyse"),
    ("organize", "organise"),
    ("recognize", "recognise"),
    ("prioritize", "prioritise"),
    ("specialize", "specialise"),
    ("customize", "customise"),
    ("maximize", "maximise"),
    ("minimize", "minimise"),
    ("personalize", "personalise"),
    ("color", "colour"),
    ("center", "centre"),
    ("catalog", "catalogue"),
    ("behavior", "behaviour"),
    ("favorite", "favourite"),
    ("license", "licence"),
    ("program", "programme"),
    ("traveled", "travelled"),
)

MIN_DESCRIPTION_CHARS = 25
MAX_DESCRIPTION_CHARS = 160
MAX_TITLE_CHARS = 60


@dataclass(slots=True)
class CopyVerdict:
    """What is wrong with one link line, if anything."""

    url: str
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def describe(self) -> str:
        return f"{self.url}: " + "; ".join(self.problems)


def _superlative_pattern(words: tuple[str, ...]) -> re.Pattern[str]:
    return re.compile(r"\b(" + "|".join(re.escape(w) for w in words) + r")\b", re.I)


def check_copy(
    url: str,
    title: str,
    description: str,
    *,
    banned_openers: tuple[str, ...] = BANNED_OPENERS,
    banned_superlatives: tuple[str, ...] = BANNED_SUPERLATIVES,
    max_title: int = MAX_TITLE_CHARS,
    min_description: int = MIN_DESCRIPTION_CHARS,
    max_description: int = MAX_DESCRIPTION_CHARS,
) -> CopyVerdict:
    """Judge one link line. Pure."""
    verdict = CopyVerdict(url=url)
    title = (title or "").strip()
    description = (description or "").strip()

    if not title:
        verdict.problems.append("no title")
    elif len(title) > max_title:
        verdict.problems.append(f"title is {len(title)} chars (max {max_title})")

    if not description:
        # The single most common defect, and the one the old link-line regex could
        # not see because it stopped matching at the colon.
        verdict.problems.append("no description")
        return verdict

    if len(description) < min_description:
        verdict.problems.append(f"description is {len(description)} chars (min {min_description})")
    elif len(description) > max_description:
        verdict.problems.append(f"description is {len(description)} chars (max {max_description})")

    lowered = description.lower()
    if opener := next((w for w in banned_openers if lowered.startswith(w)), None):
        verdict.problems.append(f"opens with {opener!r}, which describes the reader not the page")

    if found := _superlative_pattern(banned_superlatives).findall(description):
        unique = sorted({f.lower() for f in found})
        verdict.problems.append("unverifiable superlative(s): " + ", ".join(unique))

    return verdict


def check_all(entries: list) -> list[CopyVerdict]:
    """Judge every entry, including cross-entry rules a single line cannot see.

    Duplicate descriptions are the obvious one: each line is individually fine and
    the pair is useless, because an agent choosing between them has nothing to go on.
    """
    verdicts = [check_copy(e.url, e.title, e.description) for e in entries]

    seen: dict[str, str] = {}
    by_url = {v.url: v for v in verdicts}
    for entry in entries:
        key = (entry.description or "").strip().lower()
        if not key:
            continue
        if key in seen and seen[key] != entry.url:
            by_url[entry.url].problems.append(f"description duplicates {seen[key]}")
        else:
            seen[key] = entry.url

    return verdicts


def locale_conflicts(text: str) -> list[str]:
    """Words spelled both ways in the same document.

    Reports mixing, not dialect. Which spelling a site prefers is the operator's
    call; using both in one file is the defect, and it is decidable.
    """
    lowered = text.lower()
    conflicts = []
    for american, british in LOCALE_PAIRS:
        a = len(re.findall(rf"\b{american}\b", lowered))
        b = len(re.findall(rf"\b{british}\b", lowered))
        if a and b:
            conflicts.append(f"{american} x{a} / {british} x{b}")
    return conflicts
