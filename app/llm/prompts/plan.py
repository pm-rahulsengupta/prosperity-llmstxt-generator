"""Stage 1: decide what to crawl, before anything is crawled.

This is the stage the source tool did not have. There, an LLM only ever described
pages that had already been fetched -- so the crawl budget was spent before any
judgement was applied to it, and a 40,000-URL site got the same treatment as a
40-page one.

The model sees the size estimate, the robots rules, the sitemap split and the URL
templates with their counts. It never sees a page body. One cheap call, before any
crawl spend, and its output is reviewed by a human before it takes effect.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.ranking import PATTERN_LABELS, PATTERN_TEMPLATES

SYSTEM = """You plan web crawls for building llms.txt files.

An llms.txt is a curated index of the pages that best explain what a site is and \
does, for a language model reading it cold. It is not a sitemap. A good plan \
excludes far more than it includes.

You will be given a site's URL templates with page counts, its sitemap split, its \
robots.txt rules and an estimate of its size. Decide which templates to crawl, in \
what priority order, and which llms.txt section each template's pages should feed.

Judge by what a template's pages are likely to contain, not by how many there are:

- Include documentation, guides, product and service pages, high-level category \
pages, about/contact, and anything that explains the offering.
- Exclude pagination, tag and author archives, filtered or faceted listings, \
search results, cart/checkout/account pages, print or AMP variants, dated archive \
indexes, and near-identical location or comparison pages generated from a template.
- When a template holds hundreds of near-identical pages, prefer its index page \
and a handful of examples over the whole set.

Set priority 1 for pages that must appear, 5 for pages to take only if budget \
allows. Use the section names given to you; do not invent a taxonomy."""


def schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "site_name",
            "site_pattern",
            "rules",
            "requires_js",
            "recommended_page_cap",
            "reasoning",
        ],
        "properties": {
            "site_name": {
                "type": "string",
                "description": "The organisation or product name, as a reader would say it.",
            },
            "site_pattern": {
                "type": "string",
                "enum": [*PATTERN_TEMPLATES.keys()],
                "description": "Which section template best fits this site.",
            },
            "rules": {
                "type": "array",
                "description": "One rule per URL template supplied, in the order given.",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["template", "action", "priority", "section", "reason"],
                    "properties": {
                        "template": {"type": "string"},
                        "action": {"type": "string", "enum": ["include", "exclude", "sample"]},
                        "priority": {"type": "integer", "minimum": 1, "maximum": 5},
                        "section": {
                            "type": "string",
                            "description": "Target llms.txt section, or empty when excluded.",
                        },
                        "reason": {"type": "string", "description": "One short clause."},
                    },
                },
            },
            "requires_js": {
                "type": "boolean",
                "description": "True only if the site is evidently client-rendered.",
            },
            "recommended_page_cap": {
                "type": "integer",
                "description": "How many pages are worth fetching in total.",
            },
            "reasoning": {"type": "string", "description": "Two or three sentences, no more."},
        },
    }


@dataclass(slots=True)
class TemplateRule:
    template: str
    action: str = "include"
    priority: int = 3
    section: str = ""
    reason: str = ""

    @property
    def includes(self) -> bool:
        return self.action in {"include", "sample"}

    # `sample` means "this template is repetitive: take a few, not all of them".
    @property
    def sample_only(self) -> bool:
        return self.action == "sample"


@dataclass(slots=True)
class CrawlPlan:
    site_name: str = ""
    site_pattern: str = "catalog"
    rules: list[TemplateRule] = field(default_factory=list)
    requires_js: bool = False
    recommended_page_cap: int = 0
    reasoning: str = ""
    source: str = "heuristic"

    def rule_for(self, template: str) -> TemplateRule | None:
        return next((rule for rule in self.rules if rule.template == template), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "site_name": self.site_name,
            "site_pattern": self.site_pattern,
            "requires_js": self.requires_js,
            "recommended_page_cap": self.recommended_page_cap,
            "reasoning": self.reasoning,
            "source": self.source,
            "rules": [
                {
                    "template": rule.template,
                    "action": rule.action,
                    "priority": rule.priority,
                    "section": rule.section,
                    "reason": rule.reason,
                }
                for rule in self.rules
            ],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CrawlPlan:
        return cls(
            site_name=data.get("site_name", ""),
            site_pattern=data.get("site_pattern", "catalog"),
            requires_js=bool(data.get("requires_js")),
            recommended_page_cap=int(data.get("recommended_page_cap") or 0),
            reasoning=data.get("reasoning", ""),
            source=data.get("source", "heuristic"),
            rules=[
                TemplateRule(
                    template=rule.get("template", ""),
                    action=rule.get("action", "include"),
                    priority=int(rule.get("priority") or 3),
                    section=rule.get("section", ""),
                    reason=rule.get("reason", ""),
                )
                for rule in data.get("rules", [])
            ],
        )


def build_user_message(brief: str, page_cap: int) -> str:
    """`brief` is `Preflight.planning_brief()`: size estimate plus the inventory."""
    sections = "\n".join(
        f"- {pattern} ({PATTERN_LABELS[pattern]}): {', '.join(names)}"
        for pattern, names in PATTERN_TEMPLATES.items()
    )
    return (
        f"{brief}\n\n"
        f"Crawl budget: about {page_cap} pages.\n\n"
        f"Section templates available:\n{sections}\n\n"
        "Return one rule for every URL template listed above, using the template "
        "string exactly as given."
    )
