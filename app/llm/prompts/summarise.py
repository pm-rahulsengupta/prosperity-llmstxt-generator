"""Stage 3: the site blurb, and one title/description per link line.

The prompts here are ported close to verbatim from the source tool. They were the
one part of it that was demonstrably well shaped -- short, specific, with the length
constraint stated in words rather than characters, which is what actually holds a
model to a link-line-sized answer.

What changed is only the budget and the delivery: these calls were capped at 100
tokens each alongside every other stage, and the structured-output schema replaces a
hand-written JSON example pasted into the prompt.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.core.models import PageEntry

BATCH_SIZE = 25

# Enough of the body to tell what a page is, and not a token more. The source used
# 4,000 characters; 25 pages of that is 100k characters a call, so it is per-page
# content that has to be bounded, not just the batch.
CONTENT_CHARS = 1_200

SITE_SYSTEM = """You write the opening of an llms.txt file.

Given a site's name and a sample of its most important pages, write:

- a one-line summary of what the organisation does, as a blockquote sentence of \
about 20 words, no marketing language, no "we"; and
- two or three sentences of context a language model would need to use the site \
correctly: what it offers, who it is for, anything easily mistaken about it.

Write plainly. Do not repeat the site name in the summary line."""

PAGE_SYSTEM = """You write the link lines of an llms.txt file.

For each page, return:

- title: 3 to 5 words naming what the page is. Not a sentence. Strip any brand suffix. Use the page's own vocabulary.
- description: 8 to 12 words saying what the page *contains or answers*. Describe the page, not the reader's activity.

Never begin a description with Learn, Discover, Explore, Understand, Find out, Gain, Dive into or Unlock. Those name what the reader should do and say nothing about what is behind the link. Write "Token types, scopes and refresh flow", not "Learn about authentication".

Do not use unverifiable superlatives: award-winning, best, leading, top, world-class, premier, cutting-edge, proven, expert. This is a machine-readable index, not marketing copy.

Both must be specific enough that someone choosing between two pages could choose. "Learn more about our services" is a failure. Return one entry per page, keyed by the exact URL you were given."""


def site_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["site_name", "summary", "context"],
        "properties": {
            "site_name": {"type": "string"},
            "summary": {"type": "string", "description": "One blockquote sentence, ~20 words."},
            "context": {"type": "string", "description": "Two or three sentences."},
        },
    }


def page_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["pages"],
        "properties": {
            "pages": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["url", "title", "description"],
                    "properties": {
                        "url": {"type": "string"},
                        "title": {"type": "string"},
                        "description": {"type": "string"},
                    },
                },
            }
        },
    }


@dataclass(slots=True)
class SiteBlurb:
    site_name: str = ""
    summary: str = ""
    context: str = ""


@dataclass(slots=True)
class PageCopy:
    url: str
    title: str
    description: str


def build_site_message(site_url: str, site_name: str, entries: list[PageEntry]) -> str:
    lines = [f"Site: {site_url}", f"Known name: {site_name or '(unknown)'}", "", "Top pages:"]
    for entry in entries[:20]:
        lines.append(f"- {entry.url} -- {entry.display_title[:120]}")
    sample = next((e.markdown for e in entries if e.markdown), "")
    if sample:
        lines += ["", "Homepage extract:", sample[:2_000]]
    return "\n".join(lines)


def build_page_message(entries: list[PageEntry]) -> str:
    blocks = []
    for entry in entries:
        body = (entry.markdown or "")[:CONTENT_CHARS].strip()
        blocks.append(
            f"- url: {entry.url}\n"
            f"  current_title: {entry.display_title[:120]}\n"
            f"  meta_description: {(entry.description or '')[:200]}\n"
            f"  content: {body or '(no body text captured)'}"
        )
    return "\n\n".join(blocks)


def parse_pages(data: dict[str, Any], known_urls: set[str]) -> list[PageCopy]:
    return [
        PageCopy(
            url=item["url"],
            title=(item.get("title") or "").strip(),
            description=(item.get("description") or "").strip(),
        )
        for item in data.get("pages", [])
        if item.get("url") in known_urls
    ]


def parse_site(data: dict[str, Any]) -> SiteBlurb:
    return SiteBlurb(
        site_name=(data.get("site_name") or "").strip(),
        summary=(data.get("summary") or "").strip(),
        context=(data.get("context") or "").strip(),
    )
