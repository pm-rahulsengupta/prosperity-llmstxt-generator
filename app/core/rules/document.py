"""Parse an llms.txt (or llms-full.txt) once, into something rules can read.

Thirty-three rules each running their own regex over the raw text is how a rule set
becomes slow, inconsistent and impossible to reason about — two rules disagreeing on
what counts as "a section" is a bug nobody finds. Everything parses here, once, and
the rules ask questions of the result.

The spec's document order is: H1, blockquote, free-form markdown containing no
headings, then zero or more H2-delimited link lists, with `## Optional` last if it
appears. That shape is what `IndexDoc` records — including where each part *ends*,
because several rules are about what appears in the wrong place rather than what is
missing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# A link line per the spec: a list item whose content is a markdown link, optionally
# followed by a colon and a description. Deliberately permissive about what follows
# the colon so that an EMPTY description is parsed rather than rejected -- that is a
# finding for a rule to make, not a parse failure. The old validator's regex silently
# accepted a blank description because it stopped matching at the colon.
# The spec's separator is `:`, but a dash is common in the wild -- docs.anthropic.com
# writes `- [Overview](url) - Agent Skills` for all 567 of its links. The link itself
# is well formed and the description is right there, so parsing only `:` would report
# the reference implementation of the format as 89 malformed links with 478 missing
# descriptions. The separator variance is a nit for a rule to note, not a parse error.
LINK_LINE = re.compile(
    r"^\s*-\s+\[(?P<title>[^\]]*)\]\((?P<url>[^)\s]*)\)"
    # The en and em dashes are deliberate: real files use them as the separator.
    r"\s*(?:(?P<sep>:|[-–—])\s*(?P<desc>.*))?$"
)
HEADING = re.compile(r"^(?P<hashes>#{1,6})\s+(?P<text>.*)$")
ANY_MD_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
SOURCE_LINE = re.compile(r"^Source:\s*(?P<url>\S+)\s*$", re.M)

# `---`, `***` and `___` open with a list marker but are thematic breaks, and a
# `*italicised line*` is emphasis. Treating either as a malformed list item is how a
# conventional generated footer gets reported as two broken links.
THEMATIC_BREAK = re.compile(r"^\s*([-*_])(?:\s*\1){2,}\s*$")
EMPHASISED_LINE = re.compile(r"^\s*([*_])(?!\s).*\1\s*$")

# A page boundary in an llms-full.txt is an H2 whose block carries a Source: URL
# within a few lines. Every other H2 came from inside a page body.
BOUNDARY_WINDOW = 6


@dataclass(slots=True)
class Heading:
    level: int
    text: str
    line: int


@dataclass(slots=True)
class LinkItem:
    title: str
    url: str
    description: str
    line: int
    section: str
    # True when the line parsed as a list item but not as a well-formed link line.
    malformed: bool = False
    raw: str = ""

    @property
    def has_description(self) -> bool:
        return bool(self.description.strip())


@dataclass(slots=True)
class Section:
    name: str
    line: int
    links: list[LinkItem] = field(default_factory=list)
    # Any non-blank, non-link line inside the section. The spec says an H2 section
    # contains a file list; prose in the middle of one is worth noticing.
    stray_lines: list[tuple[int, str]] = field(default_factory=list)


@dataclass(slots=True)
class IndexDoc:
    """An llms.txt, parsed."""

    text: str
    lines: list[str] = field(default_factory=list)
    headings: list[Heading] = field(default_factory=list)
    h1: str = ""
    h1_line: int = -1
    blockquote: str = ""
    blockquote_line: int = -1
    intro: str = ""
    intro_headings: list[Heading] = field(default_factory=list)
    sections: list[Section] = field(default_factory=list)
    # Content appearing after the last link of the last section.
    trailing: str = ""
    trailing_line: int = -1

    @property
    def byte_size(self) -> int:
        return len(self.text.encode("utf-8"))

    @property
    def links(self) -> list[LinkItem]:
        return [link for section in self.sections for link in section.links]

    @property
    def optional_section(self) -> Section | None:
        return next((s for s in self.sections if s.name.strip().lower() == "optional"), None)

    @property
    def non_optional_links(self) -> list[LinkItem]:
        return [
            link
            for section in self.sections
            if section.name.strip().lower() != "optional"
            for link in section.links
        ]


@dataclass(slots=True)
class FullPage:
    """One page block inside an llms-full.txt."""

    title: str
    line: int
    source: str = ""
    body: str = ""
    # Headings inside the body, i.e. after the page's own `##` boundary.
    body_headings: list[Heading] = field(default_factory=list)

    @property
    def char_count(self) -> int:
        return len(self.body)


@dataclass(slots=True)
class FullDoc:
    """An llms-full.txt, parsed."""

    text: str
    lines: list[str] = field(default_factory=list)
    h1_count: int = 0
    h2_count: int = 0
    blockquote: str = ""
    pages: list[FullPage] = field(default_factory=list)
    # H2s that are not page boundaries — i.e. headings from inside page bodies that
    # were concatenated in without demotion. This is FULL-003's whole finding.
    orphan_h2s: list[Heading] = field(default_factory=list)

    @property
    def source_count(self) -> int:
        return len(SOURCE_LINE.findall(self.text))

    @property
    def byte_size(self) -> int:
        return len(self.text.encode("utf-8"))


def _headings(lines: list[str]) -> list[Heading]:
    """Headings, ignoring anything inside a fenced code block.

    A fence matters more than it looks: `# comment` on the first line of a shell
    example is not a document heading, and counting it makes H1 rules lie.
    """
    found: list[Heading] = []
    fenced = False
    for number, line in enumerate(lines):
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if fenced:
            continue
        if match := HEADING.match(line):
            found.append(
                Heading(level=len(match["hashes"]), text=match["text"].strip(), line=number)
            )
    return found


def parse_index(text: str) -> IndexDoc:
    """Parse an llms.txt into its spec-defined parts."""
    lines = text.splitlines()
    doc = IndexDoc(text=text, lines=lines, headings=_headings(lines))

    first_h1 = next((h for h in doc.headings if h.level == 1), None)
    if first_h1 is not None:
        doc.h1, doc.h1_line = first_h1.text, first_h1.line

    # The blockquote is the first `>` block after the H1 and before the first H2.
    first_h2 = next((h for h in doc.headings if h.level == 2), None)
    stop = first_h2.line if first_h2 else len(lines)
    quote: list[str] = []
    for number in range(doc.h1_line + 1 if doc.h1_line >= 0 else 0, stop):
        stripped = lines[number].strip()
        if stripped.startswith(">"):
            if doc.blockquote_line < 0:
                doc.blockquote_line = number
            quote.append(stripped.lstrip(">").strip())
        elif quote:
            break
    doc.blockquote = "\n".join(quote).strip()

    # The free-form intro: everything between the blockquote and the first H2.
    intro_start = (
        (doc.blockquote_line + len(quote)) if doc.blockquote_line >= 0 else (doc.h1_line + 1)
    )
    intro_lines = lines[max(intro_start, 0) : stop]
    doc.intro = "\n".join(intro_lines).strip()
    doc.intro_headings = [h for h in doc.headings if intro_start <= h.line < stop]

    # Sections.
    h2s = [h for h in doc.headings if h.level == 2]
    for index, heading in enumerate(h2s):
        end = h2s[index + 1].line if index + 1 < len(h2s) else len(lines)
        section = Section(name=heading.text, line=heading.line)
        for number in range(heading.line + 1, end):
            raw = lines[number]
            stripped = raw.strip()
            if not stripped:
                continue
            if THEMATIC_BREAK.match(raw) or EMPHASISED_LINE.match(raw):
                section.stray_lines.append((number, stripped))
            elif stripped.startswith(("-", "*")) and "](" not in stripped:
                # A bullet with no link syntax is prose, not a broken link. Both
                # docs.anthropic.com and ai-sdk.dev use plain bullets for non-link
                # content ("- English (en) - 567 pages"), and reporting those as
                # malformed links fails two files that are doing this properly.
                section.stray_lines.append((number, stripped))
            elif stripped.startswith(("-", "*")):
                if match := LINK_LINE.match(raw):
                    section.links.append(
                        LinkItem(
                            title=match["title"].strip(),
                            url=match["url"].strip(),
                            description=(match["desc"] or "").strip(),
                            line=number,
                            section=heading.text,
                            raw=raw,
                        )
                    )
                else:
                    section.links.append(
                        LinkItem(
                            title="",
                            url="",
                            description="",
                            line=number,
                            section=heading.text,
                            malformed=True,
                            raw=raw,
                        )
                    )
            elif not HEADING.match(raw):
                section.stray_lines.append((number, stripped))
        doc.sections.append(section)

    # Trailing content: anything after the last link line of the last section that is
    # not itself a link or a heading. A generated footer lands here.
    if doc.sections:
        last = doc.sections[-1]
        after = (last.links[-1].line if last.links else last.line) + 1
        tail = [
            (number, lines[number]) for number in range(after, len(lines)) if lines[number].strip()
        ]
        if tail:
            doc.trailing_line = tail[0][0]
            doc.trailing = "\n".join(line for _, line in tail).strip()

    return doc


def parse_full(text: str) -> FullDoc:
    """Parse an llms-full.txt into page blocks.

    Page boundaries are H2s, per the convention the working-together guide describes.
    An H2 that appears inside a page body is exactly the defect FULL-003 looks for, so
    boundaries are taken as *every* H2 and the rules judge whether that count is right
    against the number of `Source:` lines.
    """
    lines = text.splitlines()
    headings = _headings(lines)
    doc = FullDoc(
        text=text,
        lines=lines,
        h1_count=sum(1 for h in headings if h.level == 1),
        h2_count=sum(1 for h in headings if h.level == 2),
    )

    quote = [
        line.strip().lstrip(">").strip() for line in lines[:12] if line.strip().startswith(">")
    ]
    doc.blockquote = "\n".join(quote).strip()

    # Which H2s are page boundaries: those followed closely by a Source: URL. The
    # rest are headings from inside a page body, concatenated in unchanged. Treating
    # every H2 as a boundary invents hundreds of empty pages, which then get reported
    # as "563 pages have no Source URL" -- a number about the parser, not the file.
    h2s = [h for h in headings if h.level == 2]
    boundaries = [
        heading
        for heading in h2s
        if SOURCE_LINE.search(
            "\n".join(lines[heading.line + 1 : heading.line + 1 + BOUNDARY_WINDOW])
        )
    ]
    boundary_lines = {h.line for h in boundaries}
    doc.orphan_h2s = [h for h in h2s if h.line not in boundary_lines]

    for index, heading in enumerate(boundaries):
        end = boundaries[index + 1].line if index + 1 < len(boundaries) else len(lines)
        body = "\n".join(lines[heading.line + 1 : end])
        source = SOURCE_LINE.search(body)
        doc.pages.append(
            FullPage(
                title=heading.text,
                line=heading.line,
                source=source["url"] if source else "",
                body=body,
                body_headings=[h for h in headings if heading.line < h.line < end],
            )
        )

    return doc
