"""FULL-001..009 — the rules that judge an llms-full.txt.

`llms-full.txt` is not in the spec; it is a convention. These rules therefore judge
it against what makes a concatenated corpus usable to a model reading it whole,
which is mostly a question of whether the document outline survived concatenation.

The reference fixture is our own output and it is a good demonstration of what goes
wrong: 80 H1s in a document that should have one, 641 H2s of which 563 are inside
page bodies rather than page boundaries, and one testimonial repeated 43 times.
"""

from __future__ import annotations

import re
from collections import Counter

from app.core.rules.document import FullDoc
from app.core.rules.registry import Category, Rule, RuleContext, Severity, fail, ok, skip

# A block has to be long enough that repeating it is a real cost, not a coincidence.
REPEAT_MIN_CHARS = 200
REPEAT_THRESHOLD = 3
MIN_PAGE_CHARS = 3_000
# Roughly four characters per token. Good enough to decide "does this fit in a
# context window", which is the only question the budget rule is asking.
CHARS_PER_TOKEN = 4
DEFAULT_MAX_TOKENS = 200_000
_EMPHASIS_HEADING = re.compile(r"^#{1,6}\s+[*_]")
_TRAILING_WS = re.compile(r"[ \t]+$")


def _doc(ctx: RuleContext) -> FullDoc | None:
    return ctx.full  # type: ignore[return-value]


def _needs_full(ctx: RuleContext, rule_id: str):
    if ctx.full is None:
        return skip(rule_id, "no llms-full.txt supplied")
    return None


def full_001(ctx):
    if (s := _needs_full(ctx, "FULL-001")) is not None:
        return s
    doc = _doc(ctx)
    if doc.h1_count == 1:
        return ok("FULL-001")
    if doc.h1_count == 0:
        return fail("FULL-001", "No H1. The document has no title.")
    return fail(
        "FULL-001",
        f"{doc.h1_count} H1s. Each page's own H1 was concatenated in unchanged, so the "
        "document has no single title and its outline is meaningless.",
        count=doc.h1_count,
    )


def full_002(ctx):
    if (s := _needs_full(ctx, "FULL-002")) is not None:
        return s
    doc = _doc(ctx)
    pages, sources, h2s = len(doc.pages), doc.source_count, doc.h2_count
    if pages == sources == h2s:
        return ok("FULL-002")
    return fail(
        "FULL-002",
        f"{h2s} H2s, {sources} Source: lines, {pages} page block(s). These should be "
        "equal; they are not, so page boundaries cannot be found reliably.",
        count=abs(h2s - sources),
    )


def full_003(ctx):
    if (s := _needs_full(ctx, "FULL-003")) is not None:
        return s
    doc = _doc(ctx)
    # An H2 inside a page body competes with the boundary that introduced it, and a
    # body H1 outranks the document title. `orphan_h2s` is the parser's own count of
    # H2s that were not page boundaries.
    in_body = len(doc.orphan_h2s) + sum(
        1 for page in doc.pages for h in page.body_headings if h.level == 1
    )
    if not in_body:
        return ok("FULL-003")
    return fail(
        "FULL-003",
        f"{in_body} heading(s) at H1 or H2 inside page bodies. Body headings must be "
        "demoted below the page boundary or the outline is unreadable.",
        count=in_body,
        examples=[h.text for h in doc.orphan_h2s][:5],
    )


def full_004(ctx):
    if (s := _needs_full(ctx, "FULL-004")) is not None:
        return s
    doc = _doc(ctx)
    if not doc.pages:
        return fail("FULL-004", "No page blocks found.")
    missing_source = [p.title for p in doc.pages if not p.source]
    if missing_source:
        return fail(
            "FULL-004",
            f"{len(missing_source)} page(s) have no Source: URL, so a model cannot cite "
            "or link back to them.",
            count=len(missing_source),
            examples=missing_source,
        )
    return ok("FULL-004")


def full_005(ctx):
    if (s := _needs_full(ctx, "FULL-005")) is not None:
        return s
    doc = _doc(ctx)
    # Exact repeated blocks. Boilerplate that survived extraction -- a testimonial,
    # an author bio, a call to action -- appears verbatim, so an exact frequency
    # count finds it in one pass. Fuzzy matching is the wrong tool and the wrong cost.
    counts: Counter[str] = Counter()
    for page in doc.pages:
        seen_here = set()
        for block in re.split(r"\n\s*\n", page.body):
            normalised = re.sub(r"\s+", " ", block.strip())
            if len(normalised) >= REPEAT_MIN_CHARS and normalised not in seen_here:
                seen_here.add(normalised)
                counts[normalised] += 1

    repeated = [(text, n) for text, n in counts.items() if n > REPEAT_THRESHOLD]
    if not repeated:
        return ok("FULL-005")

    repeated.sort(key=lambda pair: -pair[1])
    wasted = sum(len(text) * (n - 1) for text, n in repeated)
    return fail(
        "FULL-005",
        f"{len(repeated)} block(s) repeat across more than {REPEAT_THRESHOLD} pages, "
        f"wasting roughly {wasted // CHARS_PER_TOKEN:,} tokens. Hoist them once into a "
        "shared section.",
        count=len(repeated),
        examples=[f"x{n}: {text[:90]}" for text, n in repeated[:5]],
    )


def full_006(ctx):
    if (s := _needs_full(ctx, "FULL-006")) is not None:
        return s
    doc = _doc(ctx)
    runs, run = 0, 0
    for line in doc.lines:
        if not line.strip():
            run += 1
        else:
            if run >= 3:
                runs += 1
            run = 0
    if run >= 3:
        runs += 1
    trailing = sum(1 for line in doc.lines if _TRAILING_WS.search(line))

    if not runs and not trailing:
        return ok("FULL-006")
    parts = []
    if runs:
        parts.append(f"{runs} run(s) of 3+ blank lines")
    if trailing:
        parts.append(f"{trailing} line(s) with trailing whitespace")
    return fail("FULL-006", "Whitespace noise: " + ", ".join(parts) + ".", count=runs + trailing)


def full_007(ctx):
    if (s := _needs_full(ctx, "FULL-007")) is not None:
        return s
    doc = _doc(ctx)
    wrapped = [line for line in doc.lines if _EMPHASIS_HEADING.match(line)]
    if not wrapped:
        return ok("FULL-007")
    return fail(
        "FULL-007",
        f"{len(wrapped)} heading(s) wrap their text in bold or italics. The emphasis is "
        "redundant inside a heading and leaks into the outline.",
        count=len(wrapped),
        examples=[line.strip() for line in wrapped],
    )


def full_008(ctx):
    if (s := _needs_full(ctx, "FULL-008")) is not None:
        return s
    doc = _doc(ctx)
    thin = [
        f"{p.title} ({p.char_count} chars)" for p in doc.pages if 0 < p.char_count < MIN_PAGE_CHARS
    ]
    if not thin:
        return ok("FULL-008")
    return fail(
        "FULL-008",
        f"{len(thin)} page(s) are under {MIN_PAGE_CHARS:,} characters. A page that thin "
        "adds tokens without adding retrievable content.",
        count=len(thin),
        examples=thin,
    )


def full_009(ctx):
    if (s := _needs_full(ctx, "FULL-009")) is not None:
        return s
    doc = _doc(ctx)
    tokens = len(doc.text) // CHARS_PER_TOKEN
    if tokens <= DEFAULT_MAX_TOKENS:
        return ok("FULL-009", f"~{tokens:,} tokens")
    return fail(
        "FULL-009",
        f"~{tokens:,} tokens against a {DEFAULT_MAX_TOKENS:,} budget. Past this the file "
        "cannot be pasted into a context window, which is the workflow it exists for; "
        "split it by section.",
        count=tokens,
    )


FULL_RULES: list[Rule] = [
    Rule(
        "FULL-001",
        "Exactly one H1 in the document",
        Category.FULL,
        Severity.ERROR,
        full_001,
        "Concatenating page H1s unchanged destroys the outline.",
    ),
    Rule(
        "FULL-002",
        "H2s, Source lines and pages agree",
        Category.FULL,
        Severity.ERROR,
        full_002,
        "Page boundaries must be findable by splitting on H2.",
    ),
    Rule(
        "FULL-003",
        "No H1/H2 inside page bodies",
        Category.FULL,
        Severity.ERROR,
        full_003,
        "A body heading must not outrank its own page boundary.",
    ),
    Rule(
        "FULL-004",
        "Every page has a Source URL",
        Category.FULL,
        Severity.WARNING,
        full_004,
        "Without it a model cannot cite or link back to the page.",
    ),
    Rule(
        "FULL-005",
        "No block repeated across pages",
        Category.FULL,
        Severity.WARNING,
        full_005,
        "Boilerplate repeated per page is pure token cost.",
    ),
    Rule(
        "FULL-006",
        "No whitespace noise",
        Category.FULL,
        Severity.INFO,
        full_006,
        "Cheap to fix and it inflates the file.",
    ),
    Rule(
        "FULL-007",
        "No emphasis-wrapped headings",
        Category.FULL,
        Severity.INFO,
        full_007,
        "Redundant inside a heading, and it leaks into the outline.",
    ),
    Rule(
        "FULL-008",
        "No pages below the size floor",
        Category.FULL,
        Severity.INFO,
        full_008,
        "Thin pages add tokens without adding content.",
    ),
    Rule(
        "FULL-009",
        "Within the token budget",
        Category.FULL,
        Severity.WARNING,
        full_009,
        "Past the budget the paste-into-a-chat workflow stops working.",
    ),
]
