"""The site as an OKF bundle: one file per concept, cross-linked.

Google's Open Knowledge Format v0.1 is a directory of markdown files, each
carrying YAML frontmatter, linked to each other with ordinary markdown links. Of
the six reserved fields only `type` is required; the producer decides what types
exist. There is no runtime, no SDK and no index -- an agent reads `index.md`,
scopes to a topic, and follows links the way a person clicks through a wiki.

## What a concept is here

Three types, because three is what this tool actually knows:

* **Site** -- `index.md`, the entry point. What the site is, and the sections.
* **Section** -- the groupings llms.txt already computed. This is the layer that
  makes the bundle a graph rather than a flat list, and it costs nothing: the
  sections were derived for llms.txt and were being discarded afterwards.
* **Page** -- one per crawled page, carrying the same description llms.txt
  carries and a `resource` pointing at the live URL.

A fourth type was considered and rejected. Modelling "service" or "vehicle
category" as concepts would mean inventing an ontology per client from crawled
prose, which is the kind of confident guess this tool refuses everywhere else.
The sections are a grouping the pipeline can defend; a taxonomy is not.

## Why the links go both ways

A page links up to its section and the section links down to its pages. A
one-directional graph is traversable only if the agent happens to enter at the
top, and an agent that lands on a page file -- which is what a search result or a
cited URL gives it -- would otherwise have no route to the rest of the bundle.

## On `log.md`

The spec makes it optional and this module writes it, because the alternative is
a bundle whose staleness is invisible. An agent reasoning over a stale bundle
gives confidently wrong answers, and the only defence a directory of flat files
has is saying when it was built and from what.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from app.core.models import PageEntry, Section
from app.core.text import domain_of, unquoted

__all__ = [
    "OKF_VERSION",
    "OkfBundle",
    "frontmatter",
    "render_okf",
    "slug",
]

OKF_VERSION = "0.1"

#: Reserved by the spec, in the order the spec lists them. Written out so a field
#: added here is a deliberate extension rather than a typo that silently becomes
#: part of the format.
RESERVED = ("type", "title", "description", "resource", "tags", "timestamp")

_UNSAFE = re.compile(r"[^a-z0-9]+")


@dataclass(slots=True)
class OkfBundle:
    """The generated directory, keyed by path relative to the bundle root."""

    files: dict[str, str] = field(default_factory=dict)
    concepts: int = 0

    @property
    def count(self) -> int:
        return len(self.files)


def slug(text: str, fallback: str = "untitled") -> str:
    """A filename-safe identity for a concept.

    The file path *is* the concept's address in OKF, so this has to be stable
    across regenerations: a slug that changes breaks every link pointing at it.
    It is derived from the title rather than from a counter for that reason --
    a counter reorders when a page is added.
    """
    cleaned = _UNSAFE.sub("-", (text or "").strip().lower()).strip("-")
    return cleaned[:80].rstrip("-") or fallback


def _yaml_scalar(value: str) -> str:
    """Quote where YAML would otherwise misread the value.

    Titles routinely contain a colon ("Business Car Hire: Fleet Options"), which
    unquoted turns one field into a nested mapping and makes the file unparseable
    for the exact consumer the format exists to serve.
    """
    text = " ".join((value or "").split())
    if not text:
        return '""'
    if any(ch in text for ch in ":#\"'{}[]|>&*!%@`") or text[0] in "-?,":
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return text


def frontmatter(fields: dict[str, object]) -> str:
    """YAML frontmatter, reserved keys first and in spec order.

    `type` is the only required field, so its absence is a programming error
    here rather than something to paper over with a default -- a bundle whose
    files have no type is not an OKF bundle.
    """
    if not fields.get("type"):
        raise ValueError("OKF requires a type on every concept")

    ordered = [k for k in RESERVED if k in fields]
    ordered += [k for k in fields if k not in RESERVED]

    lines = ["---"]
    for key in ordered:
        value = fields[key]
        if value in (None, "", []):
            continue
        if isinstance(value, (list, tuple)):
            items = ", ".join(_yaml_scalar(str(v)) for v in value)
            lines.append(f"{key}: [{items}]")
        else:
            lines.append(f"{key}: {_yaml_scalar(str(value))}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def _plural(n: int, noun: str) -> str:
    """`1 section`, `3 sections`. The log is read by people as well as agents."""
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _stamp(generated_on: date | None) -> str:
    if generated_on is None:
        return datetime.now(UTC).replace(microsecond=0).isoformat()
    return datetime(generated_on.year, generated_on.month, generated_on.day, tzinfo=UTC).isoformat()


def render_okf(
    site_url: str,
    site_name: str,
    site_summary: str,
    sections: list[Section],
    optional: list[PageEntry] | None = None,
    generated_on: date | None = None,
) -> OkfBundle:
    """Build the bundle: index.md, one file per section, one per page, log.md."""
    bundle = OkfBundle()
    timestamp = _stamp(generated_on)
    domain = domain_of(site_url)
    # The stored summary arrives pre-quoted on some runs; YAML is not the place
    # for a markdown blockquote marker.
    site_summary = unquoted(site_summary)
    optional = optional or []

    # Slugs are assigned once, up front, so that a link written into a section
    # file and the filename it points at cannot be derived twice and differ.
    section_slugs: dict[int, str] = {}
    taken: set[str] = set()
    for i, section in enumerate(sections):
        base = slug(section.name, f"section-{i + 1}")
        name = base
        n = 2
        while name in taken:
            name, n = f"{base}-{n}", n + 1
        taken.add(name)
        section_slugs[i] = name

    page_slugs: dict[str, str] = {}
    taken_pages: set[str] = set()
    all_pages = [(i, p) for i, s in enumerate(sections) for p in s.pages]
    all_pages += [(-1, p) for p in optional]
    for _, page in all_pages:
        base = slug(page.display_title or page.title or page.url, "page")
        name = base
        n = 2
        while name in taken_pages:
            name, n = f"{base}-{n}", n + 1
        taken_pages.add(name)
        page_slugs[page.url] = name

    # -- index.md -----------------------------------------------------------
    index_lines = [
        frontmatter(
            {
                "type": "Site",
                "title": site_name,
                "description": site_summary,
                "resource": site_url,
                "timestamp": timestamp,
                "okf_version": OKF_VERSION,
            }
        ),
        f"# {site_name}\n",
    ]
    if site_summary:
        index_lines.append(f"{site_summary}\n")
    index_lines.append(
        f"This bundle describes {domain}. Each section below is a concept file; "
        "each section links to the pages it contains.\n"
    )
    index_lines.append("## Sections\n")
    for i, section in enumerate(sections):
        note = f" — {section.description}" if section.description else ""
        index_lines.append(f"- [{section.name}](sections/{section_slugs[i]}.md){note}")
    if optional:
        index_lines.append("- [Optional](sections/optional.md) — secondary pages")
    index_lines.append("\n## History\n\n- [Change log](log.md)")
    bundle.files["index.md"] = "\n".join(index_lines).rstrip() + "\n"

    # -- one file per section ------------------------------------------------
    for i, section in enumerate(sections):
        lines = [
            frontmatter(
                {
                    "type": "Section",
                    "title": section.name,
                    "description": section.description,
                    "tags": [slug(section.name)],
                    "timestamp": timestamp,
                }
            ),
            f"# {section.name}\n",
        ]
        if section.description:
            lines.append(f"{section.description}\n")
        lines.append(f"Part of [{site_name}](../index.md).\n")
        lines.append("## Pages\n")
        for page in section.pages:
            desc = f": {page.description}" if page.description else ""
            lines.append(f"- [{page.display_title}](../pages/{page_slugs[page.url]}.md){desc}")
        bundle.files[f"sections/{section_slugs[i]}.md"] = "\n".join(lines).rstrip() + "\n"

    if optional:
        lines = [
            frontmatter(
                {
                    "type": "Section",
                    "title": "Optional",
                    "description": "Secondary pages an agent can skip when context is short.",
                    "tags": ["optional"],
                    "timestamp": timestamp,
                }
            ),
            "# Optional\n",
            "Secondary pages. Skip these first when context is short.\n",
            f"Part of [{site_name}](../index.md).\n",
            "## Pages\n",
        ]
        for page in optional:
            desc = f": {page.description}" if page.description else ""
            lines.append(f"- [{page.display_title}](../pages/{page_slugs[page.url]}.md){desc}")
        bundle.files["sections/optional.md"] = "\n".join(lines).rstrip() + "\n"

    # -- one file per page ---------------------------------------------------
    for i, page in all_pages:
        parent = "optional" if i == -1 else section_slugs[i]
        parent_title = "Optional" if i == -1 else sections[i].name
        lines = [
            frontmatter(
                {
                    "type": "Page",
                    "title": page.display_title,
                    "description": page.description,
                    "resource": page.url,
                    "tags": [slug(parent_title)],
                    "timestamp": timestamp,
                }
            ),
            f"# {page.display_title}\n",
        ]
        if page.description:
            lines.append(f"{page.description}\n")
        lines.append(f"Source: {page.url}\n")
        lines.append(f"In section [{parent_title}](../sections/{parent}.md).\n")
        bundle.files[f"pages/{page_slugs[page.url]}.md"] = "\n".join(lines).rstrip() + "\n"

    # Counted before log.md is written, because log.md is history rather than a
    # concept and the count it quotes is the count of the thing it describes.
    bundle.concepts = len(bundle.files)

    # -- log.md --------------------------------------------------------------
    bundle.files["log.md"] = (
        frontmatter(
            {
                "type": "Log",
                "title": f"{site_name} — change log",
                "description": "When this bundle was built, and from what.",
                "timestamp": timestamp,
            }
        )
        + f"\n# Change log\n\n- {timestamp} — built from a crawl of {domain}. "
        f"{_plural(bundle.concepts, 'concept')}: "
        f"{_plural(len(sections), 'section')}, {_plural(len(all_pages), 'page')}.\n"
    )

    return bundle
