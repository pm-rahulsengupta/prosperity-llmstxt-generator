"""Classify what a sitemap group *is*, from its name and a handful of URLs.

The split matters more than the taxonomy. Naming what a group is -- product
listings, buying guides, a tag archive -- is something a model does well from a
name like `AllUsed_BodyType` and six sample URLs, because sites name their
sitemaps for their own benefit and those names are honest. Guessing what
*matters* without metrics is something a model will do confidently and badly,
and it is the failure this tool already shipped once: a plan stage with no
evidence chose a documentation taxonomy for an agency site.

So the model is asked only for the first, and its answer is advisory. Metrics
confirm or overturn it, and no verdict is ever reached on intent alone -- the
classification informs the operator's review and the ordering, and cannot
exclude anything by itself.
"""

from __future__ import annotations

from typing import Any

__all__ = ["SYSTEM", "build_user_message", "parse", "schema"]

SYSTEM = """You are labelling the sitemaps of one website so a person can plan \
which parts of it belong in an llms.txt file.

For each sitemap group you are given its name, how many URLs it holds, how many \
distinct URL shapes those follow, and a sample of the URLs themselves.

Classify each group as exactly one of:

- editorial: written by a person for a reader. Articles, guides, case studies, \
research, news, documentation pages.
- faceted: machine-generated combinations of a filter or attribute. Listings by \
make, colour, location, price band, size. Individually thin and near-identical \
to each other; there are usually thousands.
- hub: a small number of pages that organise or introduce other pages. Category \
landing pages, section indexes, service overviews, the pages a site links to \
from its own navigation.
- utility: pages that exist for the mechanics of the site rather than to be \
read. Tag and author archives, pagination, search results, logins, carts, \
legal boilerplate.

Two rules about what you are and are not deciding.

You are naming what each group IS, not whether it is important or should be \
included. Do not reason about value, traffic or priority. A faceted group may \
turn out to be the most valuable part of a site and an editorial group may be \
abandoned; that is decided later, from measurements you do not have.

A high URL count with only one or two distinct URL shapes is the clearest \
signal of a faceted group. Many distinct shapes across few URLs suggests \
editorial or hub. Use the group's name as strong evidence -- sites name their \
sitemaps for their own convenience and those names are usually accurate.

If a group does not fit any of the four, say unknown. Guessing is worse than \
declining, because a wrong label is read as evidence by everyone downstream."""


def schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["groups"],
        "properties": {
            "groups": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["group_key", "intent", "reason"],
                    "properties": {
                        "group_key": {
                            "type": "string",
                            "description": "Exactly as given.",
                        },
                        "intent": {
                            "type": "string",
                            "enum": ["editorial", "faceted", "hub", "utility", "unknown"],
                        },
                        "reason": {
                            "type": "string",
                            "description": "One short clause naming the evidence used.",
                        },
                    },
                },
            }
        },
    }


def build_user_message(rows: list) -> str:
    """`rows` are `GroupRow`s; only the tier-D fields are sent.

    Verdicts and exemplars are withheld on purpose. Showing the model what the
    metrics already concluded would let it agree with them and look like
    corroboration, when the point of asking separately is to have a second
    signal that metrics can contradict.
    """
    blocks = []
    for row in rows:
        samples = "\n".join(f"    {url}" for url in row.sample_urls)
        blocks.append(
            f"- group_key: {row.group_key}\n"
            f"  urls: {row.url_count}\n"
            f"  distinct URL shapes: {row.template_diversity}\n"
            f"  listed in more than one sitemap: {row.multi_listed}\n"
            f"  sample:\n{samples}"
        )
    return (
        "Classify each of these sitemap groups.\n\n"
        + "\n".join(blocks)
        + "\n\nReturn one entry per group, using the group_key exactly as given."
    )


def parse(data: dict[str, Any], known_keys: set[str]) -> dict[str, tuple[str, str]]:
    """Keep only entries naming a group that exists.

    A model that invents a group key, or renames one, would otherwise attach a
    label to nothing and leave the real group unclassified while looking done.
    """
    valid = {"editorial", "faceted", "hub", "utility", "unknown"}
    out: dict[str, tuple[str, str]] = {}
    for entry in data.get("groups") or []:
        key = str(entry.get("group_key") or "")
        intent = str(entry.get("intent") or "").lower()
        if key in known_keys and intent in valid:
            out[key] = (intent, str(entry.get("reason") or "").strip())
    return out
