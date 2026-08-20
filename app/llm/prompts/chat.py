"""Stage 5: editing the finished file by conversation.

The obvious way to build this is to let the model rewrite the file text and store
what it returns. That would reintroduce the worst defect in the tool this one
replaced. The source's `_rebuild_llmstxt` re-derived sections from URL paths on
every rebuild, so a user's edits were discarded the next time anything was
assembled — and if chat edits raw text while pages and sections remain the source
of truth, unticking a single page afterwards throws away the whole conversation.

So a turn does not return a file. It returns *operations* against the same model
the edit form already writes to, and the file is re-rendered from that model
afterwards. Edits survive a later re-render because they are not layered on top of
the data — they are the data.

Everything the model may do is in `OPERATIONS`. It cannot invent a field, and the
schema is `strict`, so it cannot invent an operation either.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.models import PageEntry, Section

# One entry per operation: the name the model emits, and the arguments it carries.
# `url` arguments are checked against the run's own pages before anything is
# applied -- see `app.core.edits.apply_operations`.
OPERATIONS: dict[str, str] = {
    "set_site_name": "Rename the site. args: text",
    "set_site_summary": "Rewrite the blockquote summary line. args: text",
    "set_notes": "Set the prose note block under the summary. args: text (no headings)",
    "rename_section": "Rename a section, keeping its pages. args: section, text",
    "move_page": "Move one page to a different section. args: url, section",
    "set_page_copy": "Rewrite a page's link title and/or description. args: url, title, description",
    "set_optional": "Move a page into or out of ## Optional. args: url, flag",
    "exclude_page": "Remove a page from the file. args: url",
    "include_page": "Put a previously removed page back. args: url",
    "reorder_sections": "Set the section order explicitly. args: sections (ordered list)",
}

SYSTEM = """You edit a finished llms.txt file on behalf of the person who generated it.

You do not write the file. You return a list of operations, and the file is rebuilt \
from them. Return the smallest set of operations that satisfies the request.

What the file is: a curated markdown index of a site's most useful pages, read by \
an agent deciding which one or two links to fetch. Good link descriptions are the \
single highest-leverage thing in it -- 8 to 12 words, specific enough that someone \
choosing between two pages could choose. "Learn more about our services" is a \
failure. Titles are 3 to 5 words, no brand suffix.

Rules you must not break:

- Only ever reference URLs that appear in the page list you are given. Never invent \
one, never guess at one, never repair one you think looks wrong.
- Every link needs a description. Never leave one empty.
- `## Optional` is for genuinely peripheral pages -- legal boilerplate, archives, \
tag pages. It is not a bin for low-scoring ones.
- Use only the section names that exist, unless the request is explicitly to rename \
or reorder them.

If a request is ambiguous, do the conservative reading and say what you assumed. If \
a request cannot be done with the operations available, return no operations and \
explain what you would need instead. Do not silently do something adjacent."""


def schema(sections: list[str], urls: list[str]) -> dict[str, Any]:
    """Structured-output schema. Section names are enumerated; URLs are validated
    after the call rather than enumerated, because a large run has hundreds and an
    enum that long costs more than it protects."""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["reply", "operations"],
        "properties": {
            "reply": {
                "type": "string",
                "description": "One or two sentences: what you changed and why. Plain, not chirpy.",
            },
            "operations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "op",
                        "url",
                        "section",
                        "text",
                        "title",
                        "description",
                        "flag",
                        "sections",
                    ],
                    "properties": {
                        "op": {"type": "string", "enum": [*OPERATIONS]},
                        # Every field is required by `strict` mode, so unused ones
                        # are sent as empty rather than omitted.
                        "url": {"type": "string"},
                        "section": {"type": "string"},
                        "text": {"type": "string"},
                        "title": {"type": "string"},
                        "description": {"type": "string"},
                        "flag": {"type": "boolean"},
                        "sections": {
                            "type": "array",
                            "items": {"type": "string", "enum": sections or [""]},
                        },
                    },
                },
            },
        },
    }


@dataclass(slots=True)
class Operation:
    op: str
    url: str = ""
    section: str = ""
    text: str = ""
    title: str = ""
    description: str = ""
    flag: bool = False
    sections: list[str] = field(default_factory=list)

    def describe(self) -> str:
        """A human-readable line for the revision log."""
        target = self.url or self.section or self.text[:40]
        return f"{self.op}({target})" if target else self.op


@dataclass(slots=True)
class ChatTurn:
    reply: str = ""
    operations: list[Operation] = field(default_factory=list)
    # Set when the turn was refused rather than applied.
    rejected: str = ""


def build_user_message(
    request: str,
    site_name: str,
    site_summary: str,
    sections: list[Section],
    optional: list[PageEntry],
    excluded: list[str],
) -> str:
    """The current state, compactly. Not the rendered file: the model edits the
    model, so it is shown the model."""
    lines = [
        f"Site name: {site_name}",
        f"Summary: {site_summary or '(none)'}",
        "",
        "Current file, by section:",
    ]
    for section in sections:
        lines.append(f"\n## {section.name}")
        for page in section.pages:
            lines.append(
                f"- {page.url}\n    title: {page.display_title}\n    description: {page.description}"
            )

    if optional:
        lines.append("\n## Optional")
        for page in optional:
            lines.append(f"- {page.url}\n    title: {page.display_title}")

    if excluded:
        lines.append("\nCurrently excluded from the file (can be put back):")
        lines.extend(f"- {url}" for url in excluded[:40])

    lines += ["", "---", "", f"Request: {request}"]
    return "\n".join(lines)


def parse(data: dict[str, Any]) -> ChatTurn:
    operations = [
        Operation(
            op=item.get("op", ""),
            url=(item.get("url") or "").strip(),
            section=(item.get("section") or "").strip(),
            text=(item.get("text") or "").strip(),
            title=(item.get("title") or "").strip(),
            description=(item.get("description") or "").strip(),
            flag=bool(item.get("flag")),
            sections=[s for s in (item.get("sections") or []) if s],
        )
        for item in data.get("operations", [])
        if item.get("op") in OPERATIONS
    ]
    return ChatTurn(reply=(data.get("reply") or "").strip(), operations=operations)
