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
    # Longest first. Alternation is leftmost-first, so with `best` listed before
    # `best-in-class` the `\b` after `best` matched at the hyphen and the report
    # named the wrong term -- an operator then greps the file for "best" and
    # finds a word that is not the problem.
    ordered = sorted(words, key=len, reverse=True)
    return re.compile(r"\b(" + "|".join(re.escape(w) for w in ordered) + r")\b", re.I)


def opens_with_banned(description: str, banned: tuple[str, ...] = BANNED_OPENERS) -> str:
    """The banned opener this description starts with, or "".

    `startswith` on a bare stem flagged "Discovery Bay depot hours" as opening
    with `discover`, "Explorer bookings" as `explore` and "Learning resources for
    apprentices" as `learn`. Each is a real description of a real page, marked as
    a call to action -- and `enforce_copy_rules` regenerates a flagged line, so a
    correct description was rewritten and then shipped still flagged.

    The opener has to be a whole word: the next character must not be a letter.
    One definition, because there were two -- IDX-014 in the audit and
    `check_copy` in the generator -- and two copies of a rule about the same
    words is exactly how IDX-013 and IDX-015 drifted apart.
    """
    text = (description or "").strip().lower()
    for word in banned:
        if text == word or (text.startswith(word) and not text[len(word) :][:1].isalpha()):
            return word
    return ""


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

    if opener := opens_with_banned(description, banned_openers):
        verdict.problems.append(f"opens with {opener!r}, which describes the reader not the page")

    if found := superlatives_in(description, banned_superlatives):
        verdict.problems.append("unverifiable superlative(s): " + ", ".join(found))

    return verdict


def superlatives_in(description: str, banned: tuple[str, ...] = BANNED_SUPERLATIVES) -> list[str]:
    """Banned words, minus the ones that are half of a name.

    "Top" is a superlative and "Top End" is the northern third of the Northern
    Territory. redspot.com.au has a location page for it, the model wrote an
    accurate description mentioning it twice, and IDX-013 refused the file --
    which then blocked delivery on a place name.

    The test is capitalisation in company. A superlative used as a claim is
    lower-case in running text ("the best rates") or, at the start of a sentence,
    capitalised and followed by an ordinary word ("Best rates in Sydney"). A
    superlative inside a proper noun is capitalised *and* followed by another
    capitalised word: Top End, Best Western, Premier Inn. That is decidable from
    the string, needs no gazetteer, and fails in the safe direction -- a genuine
    claim written in Title Case survives, which is a missed flag rather than a
    file we refused to ship over the name of a place.
    """
    out: list[str] = []
    for match in _superlative_pattern(banned).finditer(description):
        word = match.group(0)
        if word[:1].isupper():
            after = description[match.end() :].lstrip()
            following = after.split(" ", 1)[0].strip(".,;:!?)")
            if following[:1].isupper():
                continue
        out.append(word.lower())
    return sorted(set(out))


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

    _flag_locale_conflicts(entries, by_url)
    return verdicts


def _flag_locale_conflicts(entries: list, by_url: dict[str, CopyVerdict]) -> None:
    """Attribute a mixed spelling to the lines that hold the minority form.

    `locale_conflicts` was written, tested, and called by nothing. IDX-015 caught
    the mixing at audit time -- after the file was assembled, when the only
    remaining move is to regenerate the whole run -- while `enforce_copy_rules`,
    whose entire job is "check every link line, regenerate what fails once", never
    consulted it. redspot shipped `licence` 22 times and `license` 12 times in one
    file for an Australian client.

    The minority is flagged because the majority is the document's own evidence of
    which spelling it meant, which keeps this dialect-neutral: the module reports
    mixing, not dialect, and picking the more common form imposes no view about
    which is correct. On an exact tie there is no evidence either way, so nothing
    is flagged and IDX-015 reports the conflict rather than this guessing at it.
    """
    text = " ".join(f"{e.title or ''} {e.description or ''}" for e in entries)
    if not (conflicts := locale_conflicts(text)):
        return

    lowered = text.lower()
    for american, british in LOCALE_PAIRS:
        if not any(c.startswith(f"{american} x") for c in conflicts):
            continue
        a = len(re.findall(rf"\b{american}\b", lowered))
        b = len(re.findall(rf"\b{british}\b", lowered))
        if a == b:
            continue
        minority = american if a < b else british
        majority = british if a < b else american
        for entry in entries:
            body = f"{entry.title or ''} {entry.description or ''}".lower()
            if re.search(rf"\b{minority}\b", body):
                by_url[entry.url].problems.append(
                    f"spells {minority!r} where the rest of the file uses {majority!r}"
                )


def locale_conflicts(text: str) -> list[str]:
    """Words spelled both ways in the same document.

    Reports mixing, not dialect. Which spelling a site prefers is the operator's
    call; using both in one file is the defect, and it is decidable.
    """
    lowered = text.lower()
    conflicts = []
    for american, british in LOCALE_PAIRS:
        a = _count_inflected(american, lowered)
        b = _count_inflected(british, lowered)
        if a and b:
            conflicts.append(f"{american} x{a} / {british} x{b}")
    return conflicts


#: Pairs where the two spellings are different *words*, not different spellings
#: of one word, so their inflections do not follow the pair.
#:
#: In British English the noun is `licence` and the verb is `license`, which
#: makes "licences ... licensed ... licensing" correct rather than mixed. Same
#: for `practice`/`practise` and `advice`/`advise`. Counting inflections across
#: these reported a correctly written British file as inconsistent -- a false
#: positive introduced by the fix for the false negative, which is the trade this
#: whole sweep is supposed to avoid.
#:
#: `program`/`programme` is here for a different reason: a programme of works and
#: a computer program are both correct in British English, and `programming` and
#: `programmer` belong to the second whatever the document does elsewhere.
AMBIGUOUS_PAIRS: frozenset[str] = frozenset({"license", "practice", "advice", "program"})


def _count_inflected(word: str, lowered: str) -> int:
    """Occurrences of a word, and of its inflections where they are safe to count.

    `\\b{word}\\b` counted only the exact base form, so a file using `licensed`
    twenty times beside `licence` once reported nothing: the American count was
    zero and a conflict needs both sides. `optimize`/`optimised` and
    `analyze`/`analysing` failed the same way.

    The redspot case in the module docstring -- `licence` 22 times against
    `license` 12 -- was caught only because that pair happens to appear in its
    base form. The next one would not have been.

    Inflections are skipped for `AMBIGUOUS_PAIRS`, where the two spellings are
    different words rather than different spellings.
    """
    if word in AMBIGUOUS_PAIRS:
        return len(re.findall(rf"\b{re.escape(word)}s?\b", lowered))
    stem = word.removesuffix("e")
    return len(re.findall(rf"\b{re.escape(stem)}(?:e|ed|es|ing|ation|ations)?\b", lowered))
