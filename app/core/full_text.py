"""Prepare crawled page markdown for inclusion in llms-full.txt.

A page's markdown was written to stand alone. It opens with its own H1, uses H2
for its own sections, and repeats whatever the site puts on every page. Dropped
into a concatenated document unchanged, all three become defects: 83 H1s where
there should be one, section headings competing with the page boundaries that
introduced them, and the same testimonial forty-six times.

`app.core.rules.full_rules` describes the shape the file has to take. This module
produces it, and imports that module's thresholds rather than restating them, so
the thing that generates and the thing that checks cannot drift apart.

Structure the rules require, and what each level means here:

    # Site name          <- exactly one, the document (FULL-001)
    ### Shared           <- preamble; H3 because H2 means "page boundary"
    ## Page title        <- one per page, each with a Source: (FULL-002/004)
    ### ...              <- everything from inside a page body (FULL-003)
"""

from __future__ import annotations

import re
from collections import Counter

from app.core.rules.document import HEADING, SOURCE_LINE
from app.core.rules.full_rules import REPEAT_MIN_CHARS, REPEAT_THRESHOLD

__all__ = ["hoist_repeated", "normalise_body", "split_blocks", "strip_emphasis"]

# H1 is the document title and H2 is a page boundary, so a page's own headings
# start at H3. Markdown stops at H6, so deep pages flatten at the bottom rather
# than growing a level that does not exist.
BODY_MIN_LEVEL = 3
MAX_LEVEL = 6

_FENCES = ("```", "~~~")
# Longest first: `**` must be tried before `*` or it strips one asterisk and
# leaves a lopsided heading behind.
_WRAPPERS = ("***", "___", "**", "__", "*", "_")


def _inside_fence(lines: list[str]) -> list[bool]:
    """Flag each line that belongs to a fenced code block, fence markers included.

    Matches `document._headings`, which toggles on the fence line and skips it. A
    `# comment` on the first line of a shell example is not a document heading,
    and demoting it would corrupt the sample while fixing nothing.
    """
    inside, flags = False, []
    for line in lines:
        if line.lstrip().startswith(_FENCES):
            flags.append(True)
            inside = not inside
            continue
        flags.append(inside)
    return flags


def strip_emphasis(text: str) -> str:
    """Remove emphasis that wraps a whole heading.

    `## **What is Digital PR?**` is the single most common shape in real crawled
    markdown -- a bold line the extractor read as a heading. The emphasis says
    nothing a heading level does not already say, and it leaks into the outline.
    """
    changed = True
    while changed:
        changed = False
        for marker in _WRAPPERS:
            wrapped = (
                len(text) > 2 * len(marker) and text.startswith(marker) and text.endswith(marker)
            )
            if wrapped and (inner := text[len(marker) : -len(marker)].strip()):
                text, changed = inner, True
                break

    # A heading that genuinely opens with an asterisk -- `*args`, a footnote
    # marker -- is not emphasis, but FULL-007 only inspects the first character
    # after the hashes and would call it one. Escaping satisfies the rule and
    # keeps the character visible, where stripping would silently edit the text.
    if text[:1] in {"*", "_"}:
        text = "\\" + text
    return text


def _collapse_blanks(pairs: list[tuple[str, bool]]) -> list[str]:
    """Collapse blank runs, and drop leading and trailing blank lines.

    Two blanks survive inside a fence, where they are probably code formatting --
    a gap between two Python functions is conventional, not noise. One survives
    everywhere else. FULL-006 only objects at three, so both are safe.
    """
    out: list[str] = []
    run: list[bool] = []
    for line, inside in pairs:
        if not line.strip():
            run.append(inside)
            continue
        if run and out:
            out.extend([""] * min(len(run), 2 if run[0] and inside else 1))
        run = []
        out.append(line)
    return out


def normalise_body(markdown: str) -> str:
    """Rewrite one page's markdown so it can sit under an H2 page boundary."""
    lines = markdown.splitlines()
    fenced = _inside_fence(lines)
    pairs = list(zip(lines, fenced, strict=True))

    # Shift by the shallowest heading present, so relative hierarchy survives: a
    # page running H1/H2/H3 becomes H3/H4/H5 rather than flattening. Never
    # promote -- a page whose own headings already start at H4 is left alone,
    # because raising them would assert a structure the author did not write.
    levels = [
        len(match["hashes"])
        for line, inside in pairs
        if not inside and (match := HEADING.match(line)) and match["text"].strip()
    ]
    shift = max(0, BODY_MIN_LEVEL - min(levels)) if levels else 0

    out: list[tuple[str, bool]] = []
    for line, inside in pairs:
        if inside:
            out.append((line.rstrip(), inside))
        elif match := HEADING.match(line):
            text = strip_emphasis(match["text"].strip())
            if not text:
                # An empty heading -- `<h3>` wrapping only an image, or a
                # decorative rule the extractor read as one. Emitting the hashes
                # leaves `#### ` with a trailing space, which is the whole of
                # FULL-006 for an otherwise clean file. It says nothing an
                # outline can use, so it goes rather than being emitted bare.
                continue
            level = min(MAX_LEVEL, len(match["hashes"]) + shift)
            out.append((f"{'#' * level} {text}", inside))
        elif SOURCE_LINE.match(line):
            # A body line reading `Source: https://...` -- a blog post citing a
            # study -- parses as this document's page-boundary marker and breaks
            # the page/source count FULL-002 checks. Escaping the colon renders
            # identically and stops it counting.
            out.append(("Source\\:" + line.split(":", 1)[1].rstrip(), inside))
        else:
            out.append((line.rstrip(), inside))

    return "\n".join(_collapse_blanks(out))


def split_blocks(body: str) -> list[str]:
    """Blank-line separated blocks, where a blank line inside a fence never splits.

    The rule counts blocks with a plain `\\n\\s*\\n` split, which is fine for
    counting. It is not fine here: this function's output is reassembled, and
    splitting a fenced block in half risks dropping one half and leaving the
    fence unclosed.
    """
    lines = body.splitlines()
    blocks: list[str] = []
    current: list[str] = []
    for line, inside in zip(lines, _inside_fence(lines), strict=True):
        if not line.strip() and not inside:
            if current:
                blocks.append("\n".join(current))
                current = []
            continue
        current.append(line)
    if current:
        blocks.append("\n".join(current))
    return blocks


def _key(block: str) -> str:
    return re.sub(r"\s+", " ", block.strip())


def hoist_repeated(bodies: list[str]) -> tuple[list[str], list[str]]:
    """Lift blocks that appear on more than `REPEAT_THRESHOLD` pages out of the bodies.

    Returns the hoisted blocks and the bodies with them removed. The caller emits
    them once, which is what FULL-005 asks for -- and unlike dropping them, it
    loses nothing: the words still appear in the file, just not forty-six times.

    Code is never hoisted. Two pages documenting the same snippet is not
    boilerplate, and moving a fenced block away from the prose explaining it
    makes both useless.
    """
    per_page = [split_blocks(body) for body in bodies]
    counts: Counter[str] = Counter()
    first: dict[str, str] = {}

    for blocks in per_page:
        seen: set[str] = set()
        for block in blocks:
            if block.lstrip().startswith(_FENCES):
                continue
            key = _key(block)
            if len(key) >= REPEAT_MIN_CHARS and key not in seen:
                seen.add(key)
                counts[key] += 1
                first.setdefault(key, block)

    repeated = [key for key, seen_on in counts.most_common() if seen_on > REPEAT_THRESHOLD]
    if not repeated:
        return [], bodies

    drop = set(repeated)
    trimmed = [
        "\n\n".join(block for block in blocks if _key(block) not in drop) for blocks in per_page
    ]
    return [first[key] for key in repeated], trimmed
