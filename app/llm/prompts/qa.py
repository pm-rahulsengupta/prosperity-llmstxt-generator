"""Stage 4: a spec review of the assembled file.

Runs *after* the deterministic validators in `app/core/validate.py`, never instead
of them. Nine mechanical checks -- H1 present, blockquote present, absolute URLs,
link-line shape, no HTML, size budget -- are facts, and a model is the wrong tool
for a fact you can compute. What a model adds is the judgement those checks cannot
make: whether the descriptions actually distinguish the pages, whether the sections
are the ones a reader would expect, whether anything important is missing.

Its findings are advisory and are shown as suggestions. Nothing here rewrites the
file on its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.core.models import ValidationIssue

# The whole file, up to a bound. Reviewing a truncated file produces findings about
# the truncation.
MAX_REVIEW_CHARS = 40_000

SYSTEM = """You are reviewing a finished llms.txt against the llmstxt.org spec and \
against whether it would actually help a language model use the site.

The mechanical checks have already run and their results are given to you. Do not \
repeat them. Look for what they cannot see:

- Descriptions that could describe any page on any site.
- Two link lines that a reader could not choose between.
- A section whose name does not match what is in it.
- An obviously important page absent from the file, or a trivial one given \
prominence.
- The blockquote summary failing to say what the organisation actually does.

Be specific and quote the line you mean. If the file is good, say so and return no \
findings -- a review that always finds something is a review nobody reads."""


def schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["verdict", "findings"],
        "properties": {
            "verdict": {
                "type": "string",
                "enum": ["good", "acceptable", "needs_work"],
            },
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["level", "quote", "problem", "suggestion"],
                    "properties": {
                        "level": {"type": "string", "enum": ["warning", "info"]},
                        "quote": {
                            "type": "string",
                            "description": "The exact line from the file, or empty if the finding is about an absence.",
                        },
                        "problem": {"type": "string"},
                        "suggestion": {"type": "string"},
                    },
                },
            },
        },
    }


@dataclass(slots=True)
class Review:
    verdict: str = "acceptable"
    findings: list[ValidationIssue] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.findings is None:
            self.findings = []


def build_user_message(llmstxt: str, mechanical: list[ValidationIssue]) -> str:
    checks = (
        "\n".join(f"- [{issue.level}] {issue.message}" for issue in mechanical)
        or "- all mechanical checks passed"
    )
    body = llmstxt[:MAX_REVIEW_CHARS]
    if len(llmstxt) > MAX_REVIEW_CHARS:
        body += f"\n\n[file truncated for review at {MAX_REVIEW_CHARS:,} characters]"
    return f"Mechanical check results:\n{checks}\n\n---\n\n{body}"


def parse(data: dict[str, Any]) -> Review:
    findings = []
    for item in data.get("findings", []):
        quote = (item.get("quote") or "").strip()
        problem = (item.get("problem") or "").strip()
        suggestion = (item.get("suggestion") or "").strip()
        message = problem
        if quote:
            message = f"{problem} -- {quote[:120]}"
        if suggestion:
            message = f"{message} Suggested: {suggestion}"
        findings.append(
            ValidationIssue(level=item.get("level", "info"), message=message, code="llm_review")
        )
    return Review(verdict=data.get("verdict", "acceptable"), findings=findings)
