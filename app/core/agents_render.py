"""Render an `AgentsDoc` to markdown. Byte-deterministic.

Structure follows the live file at `allbirds.com/agents.md`, read directly rather
than reconstructed from documentation: agents acting for a user, commerce protocol,
read-only browsing, policies. The non-commerce profiles are not that file with
sections deleted — they are their own shapes, because an agent arriving at a law
firm needs different instructions from one arriving at a shop, and a truncated
commerce file would read as a broken shop rather than as a services site.

The split with `app.llm` is strict and load-bearing:

* the model writes **prose** — what the site is, how an agent should behave;
* this module writes **every URL, endpoint, version and capability**, taken from
  the verified document.

That is the same division that fixed the llms.txt description problem. Asking a
model for a fact it cannot check produces a confident wrong answer, and here a
confident wrong answer is an instruction an agent will follow.

The footer records the date and names the convention. `agents.md` has no formal
specification — it is Shopify's, and Shopify changed six agent-facing endpoints
without announcement in May 2026 — so a file that claims conformance to a standard
would be overstating. It says what it followed and when.
"""

from __future__ import annotations

from datetime import date

from app.core.agents_doc import AgentsDoc, Section

__all__ = ["render_agents_liquid", "render_agents_md"]

CONVENTION_NOTE = (
    "Follows the agents.md convention as published by Shopify (May 2026). "
    "agents.md is a convention rather than a ratified specification; the machine-"
    "readable contract for commerce is UCP, at /.well-known/ucp."
)


def render_agents_md(
    doc: AgentsDoc, generated_on: date | None = None, *, facts: list | None = None
) -> str:
    """Render the document. Same input, same bytes, always.

    `facts` are operator-asserted prose the probe could not check. They render in
    their own section, each attributed and dated, and never interleaved with
    probe-derived lines -- a person vouching for a sentence is a different kind
    of claim from a probe confirming an endpoint, and an agent reading this gets
    to tell which it is looking at.
    """
    out: list[str] = []
    title = doc.site_name or doc.site_url
    out.append(f"# {title}")

    if doc.summary:
        out.append("")
        out.append(doc.summary.strip())

    for section in doc.sections:
        block = _render_section(section, doc)
        if block:
            out.append("")
            out.extend(block)

    if facts:
        out.append("")
        out.append("## Stated by the site owner")
        out.append("")
        out.append(
            "The following were provided by the site's team and have not been "
            "independently verified by this tool."
        )
        out.append("")
        out.extend(fact.render() for fact in facts)

    stamp = (generated_on or date.today()).isoformat()
    out.append("")
    out.append("---")
    out.append("")
    out.append(f"*Generated {stamp}. {CONVENTION_NOTE}*")

    return "\n".join(out) + "\n"


def _render_section(section: Section, doc: AgentsDoc) -> list[str]:
    match section:
        case Section.IDENTITY:
            return _identity(doc)
        case Section.PERSONAL_SHOPPER:
            return _personal_shopper(doc)
        case Section.COMMERCE_PROTOCOL:
            return _commerce(doc)
        case Section.AGENT_FLOW:
            return _agent_flow()
        case Section.READ_ONLY | Section.SEARCH_AND_LISTINGS | Section.API_ACCESS:
            return _read_only(section, doc)
        case Section.ATTRIBUTION:
            return _attribution(doc)
        case Section.CONTACT:
            return _contact(doc)
        case Section.POLICIES:
            return _policies(doc)
        case Section.RATE_LIMITS:
            return _rate_limits(doc)
        case Section.NOT_SUPPORTED:
            return _not_supported(doc)
    return []


def _identity(doc: AgentsDoc) -> list[str]:
    lines = ["## About this site", ""]
    lines.append(
        doc.agent_guidance.strip() if doc.agent_guidance else f"Canonical site: {doc.site_url}"
    )
    if doc.llms_txt_url and not doc.has(Section.API_ACCESS):
        lines.append("")
        lines.append(f"Content overview for language models: {doc.llms_txt_url}")
    return lines


def _personal_shopper(doc: AgentsDoc) -> list[str]:
    return [
        "## For agents acting on behalf of a user",
        "",
        "Confirm with the person you are acting for before completing any purchase.",
        "Use live pricing and availability from the endpoints below rather than cached "
        "or remembered values, and do not state product claims that are not published "
        "on this site.",
    ]


def _commerce(doc: AgentsDoc) -> list[str]:
    lines = ["## Commerce protocol (UCP)", ""]
    if doc.ucp_version:
        lines.append(f"- Protocol version: `{doc.ucp_version}`")
    if doc.ucp_supported:
        lines.append(f"- Supported versions: {', '.join(f'`{v}`' for v in doc.ucp_supported)}")
    lines.append(f"- Discovery: `GET {doc.site_url.rstrip('/')}/.well-known/ucp`")

    for capability in doc.capabilities:
        transport = capability.transport.upper() or "endpoint"
        lines.append(f"- {capability.label} ({transport}): `{capability.url}`")

    lines.append("")
    lines.append(
        "Negotiate capabilities from the discovery document rather than assuming "
        "them; only the services listed there are supported."
    )
    return lines


def _agent_flow() -> list[str]:
    return [
        "## Typical agent flow",
        "",
        "1. Discover capabilities at `/.well-known/ucp`.",
        "2. Search or browse using the read-only endpoints below.",
        "3. Build a cart through the protocol endpoint.",
        "4. Present the order to the person you are acting for.",
        "5. Complete checkout only after they approve it.",
    ]


_READ_ONLY_HEADINGS = {
    Section.READ_ONLY: "## Read-only browsing (no authentication)",
    Section.SEARCH_AND_LISTINGS: "## Search and listings",
    Section.API_ACCESS: "## API and documentation",
}


def _read_only(section: Section, doc: AgentsDoc) -> list[str]:
    lines = [_READ_ONLY_HEADINGS[section], ""]
    for capability in doc.read_only_urls:
        lines.append(f"- {capability.label}: `{capability.url}`")
    if section is Section.API_ACCESS and doc.llms_txt_url:
        lines.append(f"- Content overview for language models: `{doc.llms_txt_url}`")
    return lines


def _attribution(doc: AgentsDoc) -> list[str]:
    return [
        "## Attribution",
        "",
        f"When you quote or summarise this content, cite the source page URL on "
        f"{doc.site_url}. Do not reproduce whole articles; link to them.",
    ]


def _contact(doc: AgentsDoc) -> list[str]:
    return [
        "## Contact",
        "",
        f"- Enquiries: `{doc.contact_url}`",
        "",
        "Submit an enquiry only when the person you are acting for has asked you to, "
        "and pass on only details they have given you for that purpose.",
    ]


def _policies(doc: AgentsDoc) -> list[str]:
    lines = ["## Policies", ""]
    for policy in doc.policies:
        lines.append(f"- {policy.label}: `{policy.url}`")
    lines.append("")
    lines.append("Read these before acting on this site; they govern what is permitted.")
    return lines


def _rate_limits(doc: AgentsDoc) -> list[str]:
    return ["## Rate limits", "", doc.rate_limit_note.strip()]


def _not_supported(doc: AgentsDoc) -> list[str]:
    """What the site does *not* offer.

    The most useful section on a site with nothing published, and the reason a
    document with no verified evidence is still worth generating. An agent that
    knows there is no transaction endpoint stops looking for one instead of
    guessing at `/cart` and `/checkout`.
    """
    lines = ["## Not supported", ""]

    if not doc.transactional:
        lines.append(
            "- This site does not sell through an agent protocol. There is no cart, "
            "checkout or ordering endpoint; do not attempt to transact here."
        )
    elif not doc.has(Section.COMMERCE_PROTOCOL):
        # A shop that has not published UCP. Stated plainly rather than left to be
        # inferred from an absent section, because the inference an agent draws
        # from silence is usually "try the usual paths".
        lines.append(
            "- No agent commerce protocol is published for this site yet. Do not "
            "attempt to transact programmatically; direct the person you are acting "
            "for to the site itself."
        )

    # Only where contact was appropriate for this shape and then found missing.
    # A shop's profile never carries a contact section, so announcing its absence
    # there states a non-fact -- and a file that reports things it was never going
    # to have teaches an agent to distrust the rest of the list.
    if any(o.section is Section.CONTACT for o in doc.omitted):
        lines.append("- No contact endpoint is published for agent use.")

    lines.append(
        "- Do not submit forms, create accounts, or take any action that changes "
        "state on this site without explicit instruction from the person you are "
        "acting for."
    )
    return lines


def render_agents_liquid(doc: AgentsDoc, generated_on: date | None = None) -> str:
    """Render a Shopify theme template rather than a static file.

    Shopify serves its own `/agents.md` on every store, so on a Shopify site the
    useful artefact is an override the merchant drops into their theme at
    `templates/agents.md.liquid`, not a file they have nowhere to put.

    The context is severely restricted, and this is the constraint that decides the
    output: per Shopify's documentation only two objects exist here, `request` and
    the auto-populated `agents`. `shop`, `collections`, `product` and the rest of
    the usual theme globals are unavailable, so a template referencing them renders
    empty. Everything below is therefore either literal text or `agents.*`.
    """
    body = render_agents_md(doc, generated_on=generated_on)

    header = (
        "{%- comment -%}\n"
        "  agents.md.liquid — generated by the Prosperity AI SEO Technical "
        "Discovery Support Tool.\n"
        "  Place at: templates/agents.md.liquid\n"
        "  Serves:   /agents.md on the store's primary domain.\n"
        "\n"
        "  This replaces Shopify's default agents.md. Only `request` and `agents`\n"
        "  are available in this template; `shop`, `collections` and other theme\n"
        "  objects render empty here, so everything below is literal or agents.*.\n"
        "{%- endcomment -%}\n"
    )
    return header + body
