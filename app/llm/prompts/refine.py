"""The prompt and schema for refining a generated agents.md.

Four layers of constraint, copied from `chat.py` because they have already been
proven on the llms.txt editor:

1. `op` is a hard `enum` over `OPERATIONS`, so the model cannot name an
   operation that does not exist.
2. `url` is enumerated too. Unlike the llms.txt editor -- which validates URLs
   after the call because a large run has hundreds -- an agents.md names a
   handful, so they fit in an enum and the model cannot even *propose* one that
   is not already in the file.
3. `strict: true` with `additionalProperties: false`, so it cannot invent a
   field.
4. `parse()` drops anything whose `op` is not in the vocabulary, in case the
   schema is ever loosened.

Then `apply_refinements` refuses on top of all of that. The layers are redundant
on purpose: this output becomes instructions an agent follows on a client's site.
"""

from __future__ import annotations

from typing import Any

from app.core.refine import OPERATIONS, RefineOp

__all__ = ["SYSTEM", "build_user_message", "parse", "schema"]

SYSTEM = """You refine a finished agents.md on behalf of the person who generated it.

You do not write the file. You return a list of operations, and the file is rebuilt from them. Return the smallest set of operations that satisfies the request.

What the file is: instructions an AI agent reads before acting on a website. An agent will follow what it says. A vague description in an llms.txt is untidy; a wrong instruction here is followed, fails, and the client's site is what looks broken.

Rules you must not break:

- You cannot add a URL, an endpoint, or a capability. Not in prose, not in a stated fact, not anywhere. Every URL in this file was confirmed by a probe, and there is no operation that introduces one. If the request needs a new endpoint named, return no operations and say it must be declared in onboarding first so it can be verified.
- You may only reference URLs that already appear in the file you are shown.
- Prose should be plain and specific. "A Sydney SEO and digital PR agency" is useful. "A leading provider of world-class solutions" is not.
- `add_fact` is for something the operator has told you that no probe could check -- opening hours, a returns window, a support policy. It is recorded against their name and marked unverified in the file. Use it only for what they actually said, never to fill a gap you noticed.
- Dropping something is always safe. Adding something is what you cannot do.

If a request is ambiguous, take the conservative reading and say what you assumed. If it cannot be done with the operations available, return no operations and explain what would be needed instead. Do not silently do something adjacent."""


def schema(urls: list[str]) -> dict[str, Any]:
    """Every field required, because `strict` mode demands it.

    Unused fields are sent as empty strings rather than omitted. `url` is
    enumerated from what the file already contains -- an agents.md names few
    enough URLs for that to be cheap, and it removes a whole class of failure
    that the llms.txt editor has to catch afterwards.
    """
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["reply", "operations"],
        "properties": {
            "reply": {
                "type": "string",
                "description": (
                    "One or two sentences: what you changed and why. Plain, not chirpy. "
                    "If you refused something, say what and what would be needed."
                ),
            },
            "operations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["op", "url", "text"],
                    "properties": {
                        "op": {"type": "string", "enum": [*OPERATIONS]},
                        "url": {"type": "string", "enum": [*urls, ""] if urls else [""]},
                        "text": {"type": "string"},
                    },
                },
            },
        },
    }


def build_user_message(request: str, doc, facts: list, rendered: str) -> str:
    """The document, then the file, then the request.

    Both the model and the rendering, unlike the llms.txt editor which shows only
    the model. Here the rendered file is what the operator is looking at when
    they type, so a request like "the second paragraph is too long" only makes
    sense against it -- while the operations act on the fields, which is why both
    are present.
    """
    lines = [
        f"Site: {doc.site_name or doc.site_url} ({doc.site_url})",
        f"Summary: {doc.summary or '(none)'}",
        f"Guidance: {doc.agent_guidance or '(none)'}",
        f"Rate-limit note: {doc.rate_limit_note or '(none)'}",
        "",
        "Capabilities in the file (URLs are fixed; only labels can change):",
    ]
    for capability in [*doc.capabilities, *doc.read_only_urls]:
        lines.append(f"- {capability.label} -> {capability.url}")
    if doc.policies:
        lines.append("")
        lines.append("Policies:")
        lines.extend(f"- {p.label} -> {p.url}" for p in doc.policies)
    if facts:
        lines.append("")
        lines.append("Operator-stated facts already recorded:")
        lines.extend(f"- {f.text} (by {f.noted_by})" for f in facts)

    lines += [
        "",
        "--- the file as it renders now ---",
        rendered[:6_000],
        "",
        "---",
        "",
        f"Request: {request}",
    ]
    return "\n".join(lines)


def parse(data: dict[str, Any]) -> tuple[str, list[RefineOp]]:
    """A last filter, in case the schema is ever loosened."""
    operations = [
        RefineOp(
            op=item.get("op", ""),
            url=(item.get("url") or "").strip(),
            text=(item.get("text") or "").strip(),
        )
        for item in data.get("operations", [])
        if item.get("op") in OPERATIONS
    ]
    return (data.get("reply") or "").strip(), operations
