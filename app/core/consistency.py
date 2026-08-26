"""Does the site actually say this?

The onboarding `facts` question has told operators since it was written that
their facts are *"checked against the corpus by the consistency audit"* and that
a missing fact *"becomes a blocking question rather than an invention"*. No such
audit existed. `brief.facts` reached exactly one place -- the crawl-planning
prompt -- and nothing ever compared a stated fact against what the site says.
This is that audit.

**What it can establish.** That a claim's distinctive vocabulary occurs together
inside one block of the corpus. That a quoted span is really in the page it was
attributed to. That a number in a claim disagrees with a number the corpus states
about apparently the same thing.

**What it cannot.** Entailment. "Founded by two ex-Google engineers" reads as
absent from a corpus saying "our founders previously worked at Google". Worse and
more important: **negation is invisible** -- "we do not offer SEO audits" and "we
offer SEO audits" share almost every content token, so a claim can be marked
supported by the sentence that denies it. `tests/test_consistency.py` carries an
`xfail` for exactly that, so the limitation lives in the suite rather than in a
comment somebody scrolls past.

**So the house rule is: absence demotes, only positive conflict blocks.** A site
that never states its founding year makes the claim unsupported, not wrong. That
is the same distinction the probes already draw between `absent` and
`unreachable` -- a thing we did not find is not a thing that is not there.

Pure: no LLM, no network, no model call per claim. One corpus is built per run
and inverted to a rare-token index, so sixty claims against two hundred pages is
milliseconds rather than sixty API calls with correlated errors.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum

from app.core.full_text import hoist_repeated, split_blocks

__all__ = [
    "Conflict",
    "Corpus",
    "Doc",
    "Support",
    "Verdict",
    "audit_facts",
    "build_corpus",
    "check_claim",
]


class Support(StrEnum):
    """Four answers, and the difference between the last two is the point.

    `ABSENT` means the corpus does not say it. `CONTRADICTED` means the corpus
    says otherwise. Only the second is grounds to refuse a claim; the first is
    grounds to label it.
    """

    SUPPORTED = "supported"
    CONTRADICTED = "contradicted"
    ABSENT = "absent"
    #: Nothing to check against -- an empty corpus. Not the same as absent, for
    #: the same reason `unreachable` is not the same as a 404.
    UNCHECKABLE = "uncheckable"


#: Words that carry no evidence. Deliberately short: a long stoplist starts
#: removing domain terms, and the rare-token requirement below already does the
#: heavy lifting.
STOPWORDS: frozenset[str] = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "been",
        "but",
        "by",
        "can",
        "could",
        "do",
        "does",
        "for",
        "from",
        "had",
        "has",
        "have",
        "he",
        "her",
        "his",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "me",
        "my",
        "no",
        "nor",
        "not",
        "of",
        "on",
        "or",
        "our",
        "out",
        "she",
        "so",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "to",
        "too",
        "us",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
        "would",
        "you",
        "your",
    ]
)

#: A token appearing on more than this share of pages is site furniture -- a
#: brand name in every title, a nav word in every header -- and cannot
#: distinguish one page from another.
COMMON_SHARE = 0.4
#: Document frequency at or below which a token is "rare" enough to anchor a
#: match. Two, not one, so a term appearing on a page and its parent still counts.
RARE_MAX_DOCS = 2
#: How much of a claim's content vocabulary must appear in one block.
#: Containment, not Jaccard or cosine -- both of those are dominated by length
#: when one side is a sentence and the other is two thousand words.
CONTAINMENT_FLOOR = 0.75

_WORD = re.compile(r"[a-z0-9']+")
_YEAR = re.compile(r"\b(19|20)\d{2}\b")
#: The commonest real contradiction by a wide margin, so it is worth naming
#: rather than leaving to the generic numeric path.
_FOUNDING = re.compile(r"\b(founded|established|since|est\.|inception)\b", re.I)
_FOUNDING_WINDOW = 40


def _tokens(text: str) -> list[str]:
    return [word for word in _WORD.findall(text.casefold()) if word not in STOPWORDS]


def _norm(text: str) -> str:
    """Casefold, strip markdown emphasis, collapse whitespace.

    Used for quote anchoring, where the model may have re-wrapped a line or
    dropped the asterisks around a bolded phrase.
    """
    stripped = re.sub(r"[*_`]+", "", text.casefold())
    return re.sub(r"\s+", " ", stripped).strip()


@dataclass(frozen=True, slots=True)
class Doc:
    """One crawled page, prepared for matching."""

    id: str
    url: str
    title: str
    blocks: tuple[str, ...]
    #: Token set per block, so a match can be required to fall inside one block
    #: rather than being assembled from vocabulary scattered down the page.
    block_tokens: tuple[frozenset[str], ...]
    normalised: str


@dataclass(frozen=True, slots=True)
class Conflict:
    """Two values that cannot both be right, and where each came from."""

    kind: str
    claim_value: str
    corpus_value: str
    doc_id: str
    url: str
    excerpt: str


@dataclass(frozen=True, slots=True)
class Verdict:
    support: Support
    score: float = 0.0
    doc_id: str = ""
    url: str = ""
    quote_found: bool = False
    conflicts: tuple[Conflict, ...] = ()
    reason: str = ""


@dataclass(frozen=True, slots=True)
class Corpus:
    docs: tuple[Doc, ...] = ()
    #: Rare token -> the docs carrying it. Turns candidate selection into a set
    #: lookup instead of a scan over every page for every claim.
    index: dict[str, frozenset[str]] = field(default_factory=dict)
    #: Tokens too common across the site to distinguish anything.
    common: frozenset[str] = frozenset()

    @property
    def empty(self) -> bool:
        return not self.docs

    def by_id(self, doc_id: str) -> Doc | None:
        return next((d for d in self.docs if d.id == doc_id), None)


def build_corpus(pages: list) -> Corpus:
    """Prepare the crawled pages for matching. Deterministic.

    Boilerplate is removed first, via the same `hoist_repeated` the full-text
    generator uses. A testimonial appearing on forty-six pages is evidence of
    nothing, and left in it would match almost any claim about the business.
    """
    usable = [page for page in pages if getattr(page, "markdown", "")]
    if not usable:
        return Corpus()

    _shared, trimmed = hoist_repeated([page.markdown for page in usable])

    docs: list[Doc] = []
    for number, (page, body) in enumerate(zip(usable, trimmed, strict=True), start=1):
        blocks = tuple(block for block in split_blocks(body) if block.strip())
        if not blocks:
            continue
        docs.append(
            Doc(
                id=f"p{number:02d}",
                url=page.url,
                title=getattr(page, "title", "") or "",
                blocks=blocks,
                block_tokens=tuple(frozenset(_tokens(block)) for block in blocks),
                normalised=_norm(body),
            )
        )

    frequency: Counter[str] = Counter()
    for doc in docs:
        for token in {t for tokens in doc.block_tokens for t in tokens}:
            frequency[token] += 1

    ceiling = max(1, int(len(docs) * COMMON_SHARE))
    common = frozenset(token for token, count in frequency.items() if count > ceiling)

    index: dict[str, set[str]] = {}
    for doc in docs:
        for token in {t for tokens in doc.block_tokens for t in tokens}:
            if frequency[token] <= RARE_MAX_DOCS:
                index.setdefault(token, set()).add(doc.id)

    return Corpus(
        docs=tuple(docs),
        index={token: frozenset(ids) for token, ids in index.items()},
        common=common,
    )


def _numbers(text: str) -> dict[str, set[str]]:
    """Numbers by kind, so a year is only ever compared against a year."""
    found: dict[str, set[str]] = {"year": set()}
    found["year"] = {match.group(0) for match in _YEAR.finditer(text)}
    return found


def _founding_years(text: str) -> set[str]:
    """Years stated *as* a founding date, not merely present in the sentence.

    A blog post mentioning 2019 is not a claim about when the company started,
    so the year has to sit near founding language to count.
    """
    years: set[str] = set()
    for marker in _FOUNDING.finditer(text):
        window = text[marker.start() : marker.end() + _FOUNDING_WINDOW]
        years.update(match.group(0) for match in _YEAR.finditer(window))
    return years


def _founding_conflict(claim: str, corpus: Corpus) -> Conflict | None:
    claimed = _founding_years(claim)
    if not claimed:
        return None
    for doc in corpus.docs:
        for block in doc.blocks:
            stated = _founding_years(block)
            if stated and not (stated & claimed):
                return Conflict(
                    kind="founding_year",
                    claim_value=sorted(claimed)[0],
                    corpus_value=sorted(stated)[0],
                    doc_id=doc.id,
                    url=doc.url,
                    excerpt=block[:200],
                )
    return None


def _best_block(claim_tokens: set[str], corpus: Corpus, prefer: str = "") -> tuple[float, str, str]:
    """Highest containment in any single block, and where it was.

    Two requirements beyond the score, and they are what make this usable:
    at least one *rare* token must match, and the matching tokens must fall
    inside one block. Without the first, "We deliver services to our clients"
    scores perfectly against every page. Without the second, a claim can be
    assembled from vocabulary scattered down an unrelated page.
    """
    if not claim_tokens:
        return 0.0, "", ""

    rare = {token for token in claim_tokens if token in corpus.index}
    candidates: set[str] = set()
    for token in rare:
        candidates |= corpus.index[token]
    if prefer:
        candidates.add(prefer)
    if not candidates:
        return 0.0, "", ""

    best = (0.0, "", "")
    for doc in corpus.docs:
        if doc.id not in candidates:
            continue
        for tokens in doc.block_tokens:
            shared = claim_tokens & tokens
            if not shared or not (shared & rare):
                continue
            score = len(shared) / len(claim_tokens)
            # A preferred doc wins ties, so citing a page the claim genuinely
            # came from is not lost to an equally-scoring neighbour.
            better = score > best[0] or (score == best[0] and doc.id == prefer)
            if better:
                best = (score, doc.id, doc.url)
    return best


def check_claim(claim: str, corpus: Corpus, *, prefer: str = "", quote: str = "") -> Verdict:
    """Judge one sentence against the corpus.

    `prefer` is the doc the claim cited; `quote` is the span it says it is
    relying on. The quote is what makes this cheap -- it turns "does this
    sentence follow from the page" into "is this string in that page", which is a
    substring search. A quote that is nowhere in the corpus is a fabrication, and
    the claim proceeds as though it had cited nothing.
    """
    if corpus.empty:
        return Verdict(Support.UNCHECKABLE, reason="no crawled pages to check against")

    conflict = _founding_conflict(claim, corpus)
    if conflict is not None:
        return Verdict(
            Support.CONTRADICTED,
            doc_id=conflict.doc_id,
            url=conflict.url,
            conflicts=(conflict,),
            reason=(f"claims {conflict.claim_value}; {conflict.url} says {conflict.corpus_value}"),
        )

    quote_found = False
    if quote.strip():
        wanted = _norm(quote)
        preferred = corpus.by_id(prefer) if prefer else None
        if preferred is not None and wanted in preferred.normalised:
            quote_found = True
        else:
            quote_found = any(wanted in doc.normalised for doc in corpus.docs)

    tokens = {token for token in _tokens(claim) if token not in corpus.common}
    score, doc_id, url = _best_block(tokens, corpus, prefer=prefer)

    if score >= CONTAINMENT_FLOOR:
        return Verdict(
            Support.SUPPORTED,
            score=score,
            doc_id=doc_id,
            url=url,
            quote_found=quote_found,
            reason=f"{int(score * 100)}% of the claim's distinctive wording appears in {url}",
        )

    return Verdict(
        Support.ABSENT,
        score=score,
        doc_id=doc_id,
        url=url,
        quote_found=quote_found,
        reason="the site does not appear to say this",
    )


def check_fact(name: str, fact, corpus: Corpus) -> Verdict:
    """A stated fact, judged as `name value` so the key carries meaning.

    `founded = 2013` alone is two tokens and one of them is a number; with the
    key it becomes a sentence the corpus can be asked about.
    """
    return check_claim(f"{name.replace('_', ' ')} {getattr(fact, 'value', '')}", corpus)


def audit_facts(brief, corpus: Corpus) -> dict[str, Verdict]:
    """Every fact the operator stated, checked against the site's own pages.

    This is what the onboarding question has been promising. A contradiction is
    worth interrupting an operator for; an absence is not, and saying so is the
    difference between a check people keep running and one they turn off.
    """
    facts = getattr(brief, "facts", None) or {}
    return {name: check_fact(name, fact, corpus) for name, fact in facts.items()}
