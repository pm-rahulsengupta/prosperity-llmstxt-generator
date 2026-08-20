"""Stage 2: assign each crawled page to a section, with the heuristic as the prior.

The model is shown the deterministic score and the section the heuristics already
chose, and asked to correct it where the page's own metadata says otherwise. That
ordering matters: a model asked to sort 400 pages from scratch produces a different
taxonomy every run, while a model asked to correct a stable prior produces stable
output and only moves what is clearly misplaced.

Batched, because one call per page is both slow and needlessly expensive, and
because a batch lets the model see pages in relation to each other.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.core.models import PageEntry

# Sized so a batch's response comfortably fits the stage budget. 40 pages at roughly
# 40 tokens of output each is ~1,600 tokens against a 4,000-token allowance.
BATCH_SIZE = 40

SYSTEM = """You are organising the pages of one website into the sections of an \
llms.txt index.

Each page arrives with the section a rule-based heuristic already assigned and an \
importance score from the site's own link graph. Treat that as a well-informed \
prior. Change a page's section only when its title, description or URL clearly \
indicates a better fit, and say so briefly.

Mark a page optional when it is peripheral to understanding the site -- legal \
boilerplate, individual author or tag pages, thin utility pages -- rather than \
simply less popular. Optional is not a synonym for low score.

Use only the section names supplied. Return one entry per page, keyed by the exact \
URL you were given."""


def schema(sections: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["assignments"],
        "properties": {
            "assignments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["url", "section", "is_optional", "confidence"],
                    "properties": {
                        "url": {"type": "string"},
                        "section": {"type": "string", "enum": sections},
                        "is_optional": {"type": "boolean"},
                        "confidence": {
                            "type": "string",
                            "enum": ["high", "medium", "low"],
                            "description": "How clear-cut the placement is.",
                        },
                    },
                },
            }
        },
    }


@dataclass(slots=True)
class Assignment:
    url: str
    section: str
    is_optional: bool = False
    confidence: str = "medium"


def batches(entries: list[PageEntry], size: int = BATCH_SIZE) -> list[list[PageEntry]]:
    return [entries[i : i + size] for i in range(0, len(entries), size)]


def build_user_message(
    entries: list[PageEntry], sections: list[str], scores: dict[str, float]
) -> str:
    lines = [f"Sections available: {', '.join(sections)}", "", "Pages:"]
    for entry in entries:
        description = (entry.description or "")[:160]
        lines.append(
            f"- url: {entry.url}\n"
            f"  title: {entry.display_title[:120]}\n"
            f"  description: {description}\n"
            f"  heuristic_section: {entry.section or '(none)'}\n"
            f"  heuristic_optional: {str(entry.is_optional).lower()}\n"
            f"  importance: {scores.get(entry.url, 0.0):.1f}"
        )
    return "\n".join(lines)


def parse(data: dict[str, Any], known_urls: set[str]) -> list[Assignment]:
    """Keep only assignments for URLs we actually sent.

    A model that invents or mangles a URL must not be able to create a page, and a
    dropped page keeps its heuristic assignment rather than vanishing.
    """
    assignments = []
    for item in data.get("assignments", []):
        url = item.get("url", "")
        if url in known_urls:
            assignments.append(
                Assignment(
                    url=url,
                    section=item.get("section", ""),
                    is_optional=bool(item.get("is_optional")),
                    confidence=item.get("confidence", "medium"),
                )
            )
    return assignments
