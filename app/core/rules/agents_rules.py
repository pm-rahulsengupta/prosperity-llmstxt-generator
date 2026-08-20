"""AGT-001..014 — the rules that judge an agents.md.

They judge a different kind of failure from the IDX rules. An llms.txt with a wrong
description is read and shrugged at; an agents.md with a wrong endpoint is *acted
on*, and the action fails against the client's live site. So the severities here sit
higher than their llms.txt equivalents, and the highest of all is AGT-004: an
endpoint nobody verified.

Two things this rule set will not do.

**It does not fail a file for being unlike ours.** The IDX rules learned that from
`docs.anthropic.com/llms.txt` — 567 links, 58KB, and entirely correct. agents.md has
no ratified specification at all: it is a Shopify convention from May 2026, so
outside the commerce shape our structure is a considered opinion rather than a
standard. Profile-dependent rules skip loudly on a third-party file, and structural
rules are advisory where the convention is silent.

**It does not treat absence as failure.** A site with no UCP endpoint should have a
file that says so. That is a correct agents.md, not an incomplete one, and a
validator that marked it down would push operators toward publishing claims they
cannot support — the exact harm the generator refuses to do.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from app.core.rules.registry import Category, Rule, Severity, fail, ok, skip

# Contact details a merchant would not want handed to every agent that reads the
# file. Shopify's own documentation warns against emitting these, and the warning
# is worth taking literally: this file is fetched by anyone, forever.
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
# Deliberately narrow. The first version matched any eight-plus digit run
# containing dashes, which caught every UCP version string and the generation
# date -- `2026-04-08` reads as a phone number to a loose pattern, and a
# validator crying wolf on its own footer is one nobody keeps running.
PHONE = re.compile(
    r"(?<![\w-])(?:\+\d{1,3}[\s-]?)?(?:\(\d{2,4}\)[\s-]?|\d{2,4}[\s-])\d{3,4}[\s-]?\d{3,4}(?![\w-])"
)
ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")

# Verbs that describe changing state. Their presence on a site that cannot
# transact is the file inviting an agent to try something that will fail.
TRANSACTION_WORDS = (
    "checkout",
    "add to cart",
    "place an order",
    "complete the purchase",
    "submit payment",
)

URL_IN_TEXT = re.compile(r"https?://[^\s<>\")\]`]+")

SECTION_HEADING = re.compile(r"^##\s+(.*)$", re.M)


@dataclass(slots=True)
class AgentsContext:
    """What the AGT rules may look at.

    Its own type rather than four more fields on `RuleContext`. That one is shared
    with the IDX rules, which would then carry `verified_urls` and `transactional`
    -- concepts meaningless to an llms.txt -- and a context whose fields only apply
    to half its readers is how a rule ends up quietly reading the wrong document.

    `None` and empty mean different things throughout, as everywhere else here:
    `verified_urls=[]` says the probe found nothing, `transactional=None` says the
    site's shape is unknown. The first is evidence, the second is a skip.
    """

    text: str = ""
    site_url: str = ""
    verified_urls: list[str] = field(default_factory=list)
    transactional: bool | None = None
    content_type: str | None = None
    link_status: dict[str, int | str] = field(default_factory=dict)
    network_checked: bool = False
    probe_ran: bool = False


def _body(ctx: AgentsContext) -> str:
    return ctx.text or ""


def _urls(text: str) -> list[str]:
    return URL_IN_TEXT.findall(text)


# -- structure ---------------------------------------------------------------


def _single_h1(ctx: AgentsContext):
    text = _body(ctx)
    h1s = [line for line in text.splitlines() if line.startswith("# ")]
    if len(h1s) == 1:
        return ok("AGT-001")
    if not h1s:
        return fail("AGT-001", "No H1. An agent cannot tell whose site this describes.")
    return fail(
        "AGT-001",
        f"{len(h1s)} H1 headings; there should be one naming the site.",
        count=len(h1s),
        examples=h1s,
    )


def _has_sections(ctx: AgentsContext):
    headings = SECTION_HEADING.findall(_body(ctx))
    if headings:
        return ok("AGT-002", f"{len(headings)} section(s).")
    return fail("AGT-002", "No sections. An agent has no structure to read instructions from.")


def _not_empty(ctx: AgentsContext):
    text = _body(ctx).strip()
    if len(text) < 80:
        return fail("AGT-003", f"Only {len(text)} characters; there is nothing here to act on.")
    return ok("AGT-003")


# -- the rule that matters most ----------------------------------------------


def _no_unverified_endpoints(ctx: AgentsContext):
    """Every URL in the file must be one the probe confirmed.

    The highest-severity rule in the set. A file naming an endpoint that does not
    answer is not merely inaccurate: an agent follows it, fails, and reports the
    client's site as broken. `verified_urls` empty means the probe did not run, so
    this skips rather than condemning every URL in sight.
    """
    # `probe_ran`, not the truthiness of the list. A probe that ran and found
    # nothing is strong evidence -- every URL in the file is then unverified, which
    # is the finding -- while a probe that never ran is no evidence at all. The
    # first version tested `if not verified`, collapsing the two and silently
    # skipping the most important rule in the set on exactly the sites that need it.
    if not ctx.probe_ran:
        return skip(
            "AGT-004",
            "no probe results supplied, so no URL can be confirmed or denied",
        )

    known = {u.rstrip("/") for u in ctx.verified_urls}
    # The site's own address is verified by definition -- it is the thing being
    # described. Flagging it made the rule fire on every file we generate, since
    # the identity section names the canonical URL.
    if ctx.site_url:
        known.add(ctx.site_url.rstrip("/"))
    unverified = [u for u in _urls(_body(ctx)) if u.rstrip("/") not in known]
    if not unverified:
        return ok("AGT-004")
    return fail(
        "AGT-004",
        f"{len(unverified)} URL(s) appear in the file that no probe confirmed. An agent "
        "will follow these.",
        count=len(unverified),
        examples=unverified,
    )


def _no_transaction_language_without_an_endpoint(ctx: AgentsContext):
    """Checkout language on a site with no commerce endpoint.

    Distinct from AGT-004, which catches a bad URL. This catches prose that tells
    an agent to do something the site cannot support even though no URL is given.
    """
    transactional = ctx.transactional
    if transactional is None:
        return skip("AGT-005", "the site's profile is unknown, so its capabilities are too")
    if transactional:
        return ok("AGT-005", "the site is a shop; transaction language is expected")

    lowered = _body(ctx).lower()
    # The not-supported section exists to say "no checkout here", so its own use of
    # the word is the file working correctly rather than failing.
    head = lowered.split("## not supported")[0]
    hits = [word for word in TRANSACTION_WORDS if word in head]
    if not hits:
        return ok("AGT-005")
    return fail(
        "AGT-005",
        "The file uses transaction language on a site with no commerce endpoint.",
        count=len(hits),
        examples=hits,
    )


# -- privacy -----------------------------------------------------------------


def _no_private_contact_details(ctx: AgentsContext):
    """Shopify's documentation warns against this explicitly.

    An agents.md is fetched by anyone, indefinitely. A merchant's mobile number in
    it is a permanent disclosure made on their behalf, which is why this is an
    error rather than a note.
    """
    # Dates are stripped before the phone search rather than filtered after, so a
    # date adjacent to a real number cannot shield it.
    text = _body(ctx)
    without_dates = ISO_DATE.sub(" ", text)
    emails = EMAIL.findall(text)
    phones = [p for p in PHONE.findall(without_dates) if len(re.sub(r"\D", "", p)) >= 8]

    found = emails + phones
    if not found:
        return ok("AGT-006")
    return fail(
        "AGT-006",
        f"{len(found)} contact detail(s) published in the file. Link to a contact page "
        "instead; this file is fetched by anyone, forever.",
        count=len(found),
        examples=found,
    )


# -- links -------------------------------------------------------------------


def _links_resolve(ctx: AgentsContext):
    if not ctx.network_checked:
        return skip("AGT-007", "link checking did not run")
    broken = [
        u for u, status in ctx.link_status.items() if not (isinstance(status, int) and status < 400)
    ]
    if not broken:
        return ok("AGT-007", f"{len(ctx.link_status)} link(s) resolve.")
    return fail(
        "AGT-007",
        f"{len(broken)} link(s) do not resolve. An agent following them gets an error.",
        count=len(broken),
        examples=broken,
    )


def _absolute_urls(ctx: AgentsContext):
    """A relative path is ambiguous once the file is fetched out of context."""
    relative = [
        line.strip()
        for line in _body(ctx).splitlines()
        if re.search(r"`/(?!/)[^`]*`", line) and "://" not in line and line.strip().startswith("-")
    ]
    if not relative:
        return ok("AGT-008")
    return fail(
        "AGT-008",
        f"{len(relative)} line(s) give a path rather than a full URL.",
        count=len(relative),
        examples=relative,
    )


def _same_origin(ctx: AgentsContext):
    """Links off the site, other than a declared protocol endpoint.

    Shopify's own file points at `*.myshopify.com`, which is the store, so a
    different host is not automatically wrong — but it is worth a human look,
    because a file quietly directing agents elsewhere is how a hijack looks.
    """
    site = ctx.site_url
    if not site:
        return skip("AGT-009", "the site URL was not supplied")
    host = urlparse(site).netloc.lower().removeprefix("www.")
    # Hosts the site's own UCP document points at are the site's infrastructure,
    # on the site's own authority. Shopify's profile names `*.myshopify.com`, so
    # without this the rule fires on every correctly configured store -- flagging
    # the platform's canonical endpoint as a possible hijack.
    trusted = {host, "ucp.dev"} | {urlparse(u).netloc.lower() for u in ctx.verified_urls if u}
    offsite = [
        u
        for u in _urls(_body(ctx))
        if not any(t and t in urlparse(u).netloc.lower() for t in trusted)
    ]
    if not offsite:
        return ok("AGT-009")
    return fail(
        "AGT-009",
        f"{len(offsite)} link(s) point off {host}. Confirm each is the site's own infrastructure.",
        count=len(offsite),
        examples=offsite,
    )


# -- convention ---------------------------------------------------------------


def _states_what_is_not_supported(ctx: AgentsContext):
    """The section an agent needs most when a site offers little.

    Without it an agent infers from silence, and the inference it draws is
    usually "try the usual paths" — /cart, /checkout, /api.
    """
    if "## Not supported" in _body(ctx):
        return ok("AGT-010")
    return fail(
        "AGT-010",
        "No 'Not supported' section. An agent will guess at what it cannot do.",
    )


def _does_not_claim_a_specification(ctx: AgentsContext):
    """agents.md has no ratified spec; claiming conformance overstates."""
    lowered = _body(ctx).lower()
    claims = [
        phrase
        for phrase in ("conforms to the agents.md spec", "compliant with agents.md")
        if phrase in lowered
    ]
    if not claims:
        return ok("AGT-011")
    return fail(
        "AGT-011",
        "The file claims conformance to a specification that does not exist.",
        examples=claims,
    )


def _dated(ctx: AgentsContext):
    """Shopify changed six agent-facing endpoints without announcement in May 2026.

    A file with no date gives a reader no way to judge how stale its claims are.
    """
    if re.search(r"\b20\d{2}-\d{2}-\d{2}\b", _body(ctx)):
        return ok("AGT-012")
    return fail("AGT-012", "No generation date, so a reader cannot tell how current this is.")


def _points_at_llms_txt(ctx: AgentsContext):
    """The two files are siblings and each is more useful for knowing the other."""
    text = _body(ctx)
    if "llms.txt" in text:
        return ok("AGT-013")
    return fail(
        "AGT-013",
        "No pointer to llms.txt. An agent wanting content rather than actions has nowhere to go.",
    )


def _published_correctly(ctx: AgentsContext):
    """How the file is served, not what it says."""
    content_type = ctx.content_type
    if content_type is None:
        return skip("AGT-014", "the file was not fetched, so its content type is unknown")
    if "html" in content_type:
        return fail(
            "AGT-014",
            f"Served as {content_type!r}. A 200 of HTML is a page, not a file — agents "
            "will treat this as not published.",
        )
    if content_type and not any(
        t in content_type for t in ("text/markdown", "text/plain", "text/x-markdown")
    ):
        return fail("AGT-014", f"Served as {content_type!r}; expected text/markdown or text/plain.")
    return ok("AGT-014")


AGENTS_RULES: list[Rule] = [
    Rule(
        "AGT-001",
        "One H1 naming the site",
        Category.INDEX,
        Severity.ERROR,
        _single_h1,
        "An agent must know whose site it is reading.",
    ),
    Rule(
        "AGT-002",
        "Has sections",
        Category.INDEX,
        Severity.ERROR,
        _has_sections,
        "Instructions need structure to be followed.",
    ),
    Rule(
        "AGT-003",
        "Not empty",
        Category.INDEX,
        Severity.ERROR,
        _not_empty,
        "A near-empty file is worse than none: it looks answered.",
    ),
    Rule(
        "AGT-004",
        "No unverified endpoint",
        Category.INDEX,
        Severity.ERROR,
        _no_unverified_endpoints,
        "An agent acts on these. A wrong endpoint makes the client's site look broken.",
    ),
    Rule(
        "AGT-005",
        "No transaction language without a commerce endpoint",
        Category.INDEX,
        Severity.ERROR,
        _no_transaction_language_without_an_endpoint,
        "Inviting an agent to buy where it cannot wastes its attempt and misleads the user.",
    ),
    Rule(
        "AGT-006",
        "No private contact details",
        Category.INDEX,
        Severity.ERROR,
        _no_private_contact_details,
        "Shopify warns against this: the file is public and permanent.",
    ),
    Rule(
        "AGT-007",
        "Links resolve",
        Category.INDEX,
        Severity.ERROR,
        _links_resolve,
        "A dead link in an instruction file is a failed action.",
    ),
    Rule(
        "AGT-008",
        "Absolute URLs",
        Category.INDEX,
        Severity.WARNING,
        _absolute_urls,
        "A relative path is ambiguous once the file is read out of context.",
    ),
    Rule(
        "AGT-009",
        "Links stay on the site",
        Category.INDEX,
        Severity.WARNING,
        _same_origin,
        "A file directing agents elsewhere is worth a human look.",
    ),
    Rule(
        "AGT-010",
        "States what is not supported",
        Category.INDEX,
        Severity.WARNING,
        _states_what_is_not_supported,
        "Silence is read as an invitation to guess.",
    ),
    Rule(
        "AGT-011",
        "Claims a convention, not a specification",
        Category.INDEX,
        Severity.WARNING,
        _does_not_claim_a_specification,
        "agents.md has no ratified spec; claiming one overstates.",
    ),
    Rule(
        "AGT-012",
        "Dated",
        Category.INDEX,
        Severity.INFO,
        _dated,
        "The convention moves; a reader needs to know how stale this is.",
    ),
    Rule(
        "AGT-013",
        "Points at llms.txt",
        Category.INDEX,
        Severity.INFO,
        _points_at_llms_txt,
        "The two files are siblings and each helps a reader find the other.",
    ),
    Rule(
        "AGT-014",
        "Served as markdown or plain text",
        Category.INDEX,
        Severity.ERROR,
        _published_correctly,
        "A 200 of HTML is a soft-404 and agents treat it as absent.",
    ),
]
