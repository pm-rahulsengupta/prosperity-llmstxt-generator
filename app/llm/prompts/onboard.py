"""Propose answers to the onboarding brief from evidence about the site.

Thirteen questions is a form nobody finishes. GEO Tracker hit the same wall and
its wizard header records the fix: it replaced a four-step wizard with one call
that returns everything pre-filled, and the person reviews and edits rather than
composing from scratch. Reviewing a wrong answer takes seconds; writing a right
one from an empty box takes minutes, and the difference is whether the brief ever
gets filled in at all.

So this proposes and never decides. Every answer lands in the form as an editable
default with the reasoning attached, and nothing is saved until a person presses
the button.

Three fields are deliberately withheld from the model.

* **`ai_bot_policy`** — whether a client permits AI training on their content is a
  commercial and legal decision with a real cost attached: on Cloudflare, refusing
  training also costs Googlebot from 15 September 2026. A model has no basis for
  it and a plausible default would be adopted unread.
* **`embargoed`** — nothing in a crawl reveals what is under NDA. A guess here
  either leaks or over-redacts, and both are worse than an empty field.
* **The declared endpoints** — an MCP server the model invents would be published
  as fact. These come from the operator or not at all.

What it is good at is the rest: reading what a site is for, who it serves, which
URL patterns carry the value, and which are archive noise. Those are judgements
from evidence, which is the task models do well, and each is checkable at a glance
by the person who knows the client.
"""

from __future__ import annotations

from typing import Any

__all__ = ["SYSTEM", "build_user_message", "parse", "schema"]

SYSTEM = """You are helping an SEO consultant fill in a short brief about a \
client's website, so that a tool can generate agent-facing files for it.

You will be given what a crawl found: the site's own description of itself, its \
sitemap groups with URL counts, sample URLs, and the platform it runs on.

Propose an answer to each field. The consultant reviews and edits everything \
before it is saved, so a well-reasoned proposal they can correct is far more \
useful than a blank they must fill in. Say what the evidence supports.

Two rules about the primary action.

Choose the ONE thing the business most wants an AI agent to do for a visitor. \
Not what the site contains — what it wants to happen. A law firm's site is full \
of articles and still wants an enquiry. A store's blog exists to sell shoes. \
Where a site plainly does both, choose the one that earns the money.

Do not choose a buying action unless the site actually sells online. A brochure \
site for a manufacturer that lists distributors is not a shop, and telling an \
agent it can transact there wastes the attempt.

For the URL patterns, work from the sample URLs rather than inventing paths.

Sitemap group names are NOT paths. `post-sitemap1.xml` is the name of a file \
listing the posts; it is not a pattern that matches them. The pattern for those \
is whatever their URLs have in common. Write every pattern as a glob against the \
URL path, like /services/* or /tag/*, and check it against the sample URLs \
before proposing it. A pattern that matches nothing is worse than no pattern, \
because it reads as a considered decision and gets approved untested.

Valuable patterns are the pages a buyer or researcher needs: services, case \
studies, products, guides. Low-value patterns are archives and machinery: tag \
pages, author pages, pagination, search results, print views.

Keep every piece of prose short and factual. No marketing language, no \
superlatives, and never open a description with Learn, Discover or Explore. \
Write in Australian English.

If the evidence does not support an answer, return an empty string for it rather \
than a plausible guess. An empty field is read as "we did not know"; a wrong one \
is adopted without being checked."""


def schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        # Every property, not just the ones we care about. OpenAI's structured
        # outputs rejects a schema whose `required` omits any key in
        # `properties`, and the rejection arrives as a 400 that the client
        # swallows into a heuristic fallback -- so the feature silently does
        # nothing rather than failing visibly. Optionality is expressed by
        # allowing an empty string or list, which is what the prompt asks for.
        "required": [
            "primary_action",
            "found_for",
            "audience",
            "valuable",
            "noise",
            "must_appear",
            "rate_limit_note",
            "reasoning",
        ],
        "properties": {
            "primary_action": {
                "type": "string",
                "enum": [
                    "contact_local_business",
                    "contact_agency",
                    "book_appointment",
                    "shop_on_store",
                    "find_local_inventory",
                    "read_and_cite",
                    "use_the_api",
                    "",
                ],
                "description": "The one action the business most wants an agent to take.",
            },
            "found_for": {
                "type": "string",
                "description": "What the site should be found for. One short sentence.",
            },
            "audience": {
                "type": "string",
                "description": "Who reads answers about this site. One short sentence.",
            },
            "valuable": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Path globs worth protecting, from the sitemap evidence.",
            },
            "noise": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Path globs that are archives or machinery.",
            },
            "must_appear": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Absolute URLs that must appear whatever the traffic says.",
            },
            "rate_limit_note": {
                "type": "string",
                "description": "Suggested crawl-rate guidance, or empty.",
            },
            "reasoning": {
                "type": "string",
                "description": "Two sentences on why this action and these patterns.",
            },
        },
    }


def build_user_message(
    site_url: str,
    site_summary: str,
    platform: str,
    groups: list[tuple[str, int]],
    sample_urls: list[str],
    homepage_text: str = "",
) -> str:
    """Everything known about the site, and nothing about what we want to hear.

    The existing brief is deliberately not shown. A model given the current
    answers agrees with them, and a proposal that agrees with what is already
    there tells the consultant nothing they did not have.
    """
    lines = [f"Site: {site_url}"]
    if platform and platform != "unknown":
        lines.append(f"Platform: {platform}")
    if site_summary:
        lines.append(f"How the site describes itself: {site_summary}")

    if groups:
        lines.append("")
        lines.append("Sitemap groups, largest first:")
        for name, count in groups[:20]:
            lines.append(f"  {name} — {count:,} URLs")

    if sample_urls:
        lines.append("")
        lines.append("Sample URLs:")
        for url in sample_urls[:30]:
            lines.append(f"  {url}")

    if homepage_text:
        lines.append("")
        lines.append("Homepage text (truncated):")
        lines.append(homepage_text[:2000])

    lines.append("")
    lines.append(
        "Propose the brief. Leave any field empty where the evidence does not support an answer."
    )
    return "\n".join(lines)


def keep_matching(patterns: list[str], known_urls: list[str]) -> tuple[list[str], list[str]]:
    """Drop proposed globs that match none of the site's URLs.

    Measured on the first live run: given sitemap group names alongside sample
    URLs, the model proposed `/post-sitemap1.xml` as a low-value *path* pattern.
    It is a sitemap filename, not a path, and as a glob it matches exactly one
    thing -- the sitemap. A pattern matching nothing is worse than no pattern,
    because the form presents it as a considered decision and the operator
    approves it without testing it.

    The prompt already says this. Prompts are not enforcement, so the check lives
    here: ask the model for judgement, verify with code. Same split as the copy
    rules and the endpoint claims.
    """
    from app.core.onboarding import matches_any

    kept, dropped = [], []
    for pattern in patterns:
        if any(matches_any(url, (pattern,)) for url in known_urls):
            kept.append(pattern)
        else:
            dropped.append(pattern)
    return kept, dropped


def parse(data: dict[str, Any]) -> dict[str, Any]:
    """Normalise into the shape `brief_from_answers` expects.

    Lists are joined into newline-delimited text because that is what the form
    posts, so a suggestion and a typed answer travel the same path and there is
    only one parser to be wrong.
    """

    def lines(key: str) -> str:
        values = data.get(key) or []
        if isinstance(values, str):
            values = [values]
        return "\n".join(str(v).strip() for v in values if str(v).strip())

    return {
        "primary_action": str(data.get("primary_action") or "").strip(),
        "found_for": str(data.get("found_for") or "").strip(),
        "audience": str(data.get("audience") or "").strip(),
        "valuable": lines("valuable"),
        "noise": lines("noise"),
        "must_appear": lines("must_appear"),
        "rate_limit_note": str(data.get("rate_limit_note") or "").strip(),
        "_reasoning": str(data.get("reasoning") or "").strip(),
    }
