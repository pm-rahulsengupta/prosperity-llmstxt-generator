"""Conversational edits to a generated agents.md, as operations on the document.

The obvious build is to let a model rewrite the file and store what it returns.
`app/llm/prompts/chat.py` already explains why that is wrong for llms.txt, and
the reasoning is stronger here: agents.md contains *instructions an agent will
follow*, so a wrong line is not untidy, it is acted on. A model with a text box
and a client's web root at the other end is the failure this whole tool is built
to prevent.

So a turn returns operations against `AgentsDoc`, the file is re-rendered from
the document, and edits survive a re-probe because they are not layered on top of
the data -- they are the data.

## What an operation may touch

Every operation is either **prose** or a **narrowing**. Neither can introduce a
claim.

* Prose: the summary, the guidance line, the rate-limit note, the site name, a
  capability's human label. `AgentsDoc` already records that `summary` and
  `agent_guidance` are *"Written by the LLM. Prose only -- it never supplies a
  URL or an endpoint."* This extends that boundary rather than crossing it.
* Narrowing: dropping a capability or a policy. Removing something a probe found
  is always safe -- the result claims strictly less than the evidence supports.

There is deliberately **no operation that adds a URL, an endpoint, or a
capability.** An operator who has stood up an MCP server declares it in
onboarding, `verify_declared` confirms it answers, and only then may the file
name it. That path already exists and is the only one.

## Operator-asserted facts

The one place a human may add something a probe cannot check: a prose fact, such
as a returns window or a support-hours note. These are useful, they are not
verifiable, and the difference has to survive into the file. So they render in
their own section, marked unverified and dated, and never mixed in with
probe-derived lines. An operator vouching for a sentence is a different kind of
claim from a probe confirming an endpoint, and a reader -- human or agent -- gets
to tell. The operator's *name* stays in the tool: AGT-006 rightly refuses to let
this file carry an email address.

A fact containing a URL is refused. That is the boundary above, restated where
somebody would otherwise walk around it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

__all__ = ["OPERATIONS", "AssertedFact", "RefineOp", "RefineReport", "apply_refinements"]

URL_IN_TEXT = re.compile(r"https?://|www\.", re.I)

#: The whole vocabulary. A model cannot name an operation that is not here --
#: the schema enumerates these -- and cannot invent a field, because every
#: operation reads only from the names below.
OPERATIONS: dict[str, str] = {
    "set_summary": "Rewrite the one-line summary. args: text",
    "set_guidance": "Rewrite the guidance line agents read first. args: text",
    "set_site_name": "Rename the site as it appears in the file. args: text",
    "set_rate_limit_note": "Rewrite the rate-limit note. args: text",
    "relabel_capability": "Rename a capability, keeping its URL. args: url, text",
    "drop_capability": "Remove a capability from the file. args: url",
    "drop_policy": "Remove a policy link from the file. args: url",
    "add_fact": "Add an operator-asserted prose fact. args: text (no URLs)",
    "clear_facts": "Remove every operator-asserted fact.",
}

MAX_FACT = 300
MAX_PROSE = 600


@dataclass(frozen=True, slots=True)
class AssertedFact:
    """Something a person vouched for, and no probe could check.

    `noted_by` is recorded for the same reason `ComponentMark` records it: this
    reaches a client's web root, and "someone said so in August" needs a name
    when a client asks who. It is kept in the database and shown in the UI, and
    deliberately does *not* reach the rendered file -- see `render`.
    """

    text: str
    noted_by: str
    noted_at: str

    def render(self) -> str:
        """Attributed as operator-stated and dated -- but never by name.

        The first version put `noted_by` in the line, and AGT-006 caught it
        before it shipped: that field is an email address, and this file is, in
        the rule's own words, "fetched by anyone, forever". Publishing a
        colleague's address to make a provenance note read better is a bad
        trade.

        What has to survive into the file is that a human asserted this and no
        probe checked it, so a reader can tell it apart from a finding. Who
        asserted it is a question for the tool, and `noted_by` is shown in the UI
        and kept in the database for exactly that.
        """
        return f"- {self.text} _(stated by the site owner {self.noted_at}; not independently verified)_"


@dataclass(frozen=True, slots=True)
class RefineOp:
    op: str
    url: str = ""
    text: str = ""


@dataclass(slots=True)
class RefineReport:
    """What was applied and what was refused, per operation."""

    applied: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.applied)


def apply_refinements(
    doc, facts: list[AssertedFact], operations: list[RefineOp], *, author: str
) -> tuple[object, list[AssertedFact], RefineReport]:
    """Apply what is allowed, refuse the rest, and say which was which.

    Returns a new document rather than mutating: the caller re-renders and audits
    the result before deciding whether to keep it, and an in-place edit would
    leave a rejected turn half-applied.

    Refusals are returned rather than raised. A model asking for one impossible
    thing among five reasonable ones should get the four, and be told about the
    fifth.
    """
    report = RefineReport()
    updated = doc
    working = list(facts)

    for operation in operations:
        outcome = _apply_one(updated, working, operation, author)
        if isinstance(outcome, str):
            report.rejected.append(outcome)
            continue
        updated = outcome
        report.applied.append(_describe(operation))

    return updated, working, report


def _apply_one(doc, facts: list[AssertedFact], operation: RefineOp, author: str):
    """Returns the new doc, or a string explaining the refusal."""
    text = operation.text.strip()
    url = operation.url.strip().rstrip("/")

    match operation.op:
        case "set_summary" | "set_guidance" | "set_site_name" | "set_rate_limit_note":
            if not text:
                return f"{operation.op} needs text, and none was given"
            if len(text) > MAX_PROSE:
                return f"{operation.op}: {len(text)} characters is past the {MAX_PROSE} limit"
            if URL_IN_TEXT.search(text):
                return (
                    f"{operation.op} may not contain a URL. Declare the endpoint in "
                    "onboarding so it can be verified, and it will appear on its own."
                )
            field_name = {
                "set_summary": "summary",
                "set_guidance": "agent_guidance",
                "set_site_name": "site_name",
                "set_rate_limit_note": "rate_limit_note",
            }[operation.op]
            return replace(doc, **{field_name: text})

        case "relabel_capability":
            if not text:
                return "relabel_capability needs the new label"
            for bucket in ("capabilities", "read_only_urls"):
                items = list(getattr(doc, bucket))
                for index, item in enumerate(items):
                    if item.url.rstrip("/") == url:
                        items[index] = replace(item, label=text)
                        return replace(doc, **{bucket: items})
            return f"no capability with the URL {operation.url} is in this file"

        case "drop_capability":
            for bucket in ("capabilities", "read_only_urls"):
                items = [i for i in getattr(doc, bucket) if i.url.rstrip("/") != url]
                if len(items) != len(getattr(doc, bucket)):
                    return replace(doc, **{bucket: items})
            return f"no capability with the URL {operation.url} is in this file"

        case "drop_policy":
            items = [p for p in doc.policies if p.url.rstrip("/") != url]
            if len(items) == len(doc.policies):
                return f"no policy with the URL {operation.url} is in this file"
            return replace(doc, policies=items)

        case "add_fact":
            if not text:
                return "add_fact needs text"
            if len(text) > MAX_FACT:
                return f"add_fact: {len(text)} characters is past the {MAX_FACT} limit"
            if URL_IN_TEXT.search(text):
                return (
                    "a stated fact may not contain a URL. An agent follows URLs, so "
                    "they come from the probe or from a verified declaration, never "
                    "from a sentence somebody typed."
                )
            facts.append(
                AssertedFact(
                    text=text,
                    noted_by=author,
                    noted_at=datetime.now(UTC).date().isoformat(),
                )
            )
            return doc

        case "clear_facts":
            facts.clear()
            return doc

    return f"{operation.op} is not an operation this file supports"


def _describe(operation: RefineOp) -> str:
    if operation.url:
        return f"{operation.op} {operation.url}"
    if operation.text:
        return f"{operation.op}: {operation.text[:60]}"
    return operation.op
