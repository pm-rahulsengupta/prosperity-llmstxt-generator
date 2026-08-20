"""The `agents.md` content model: what a site lets an agent do, and what it does not.

`llms.txt` describes what a site *is* so a model can read it. `agents.md` describes
how an agent should *act* on it. That difference decides everything here. A wrong
description in `llms.txt` is untidy; a wrong instruction here is followed, fails,
and makes the client's site look broken to every assistant that read it.

So the module has one rule and the rest is bookkeeping:

    **Nothing is stated that a probe did not verify.**

Every endpoint, policy link and capability arrives from `app.scrape.agents_probe`
carrying evidence. There is no default, no inferred convention, no "most Shopify
stores have one". A site with no verified MCP endpoint produces a file that does not
mention MCP -- and the report says the section was omitted and why, so the silence
is legible rather than looking like an oversight.

The shape is profile-driven, reusing the ten profiles in `app.core.ranking`. That
matters more here than for `llms.txt`: the Shopify template is commerce-shaped, with
UCP discovery and a checkout flow, and most client sites are not shops. Handing a
law firm a file describing cart operations would be worse than handing it nothing.
A profile that cannot transact says so explicitly, because "no transaction endpoint"
is itself useful for an agent to know.

Pure: no I/O, no network, no LLM. `app.core.metrics` and `app.core.rules` are laid
out the same way, and for the same reason -- the decisions stay testable without
credentials.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from app.core.ranking import (
    PATTERN_AGENCY,
    PATTERN_CATALOG,
    PATTERN_ECOMMERCE,
    PATTERN_ECOMMERCE_RETAIL,
    PATTERN_INDEX_EXPORT,
    PATTERN_LOCAL,
    PATTERN_PROFESSIONAL,
    PATTERN_PUBLISHER,
    PATTERN_SAAS,
    PATTERN_WORKFLOW,
)
from app.scrape.agents_probe import ProbeResult

__all__ = [
    "PROFILE_SECTIONS",
    "AgentsDoc",
    "Capability",
    "OmittedSection",
    "PolicyLink",
    "Section",
    "build_agents_doc",
    "transacts",
]


class Section(StrEnum):
    """The sections an `agents.md` can carry.

    Named after what they tell an agent to do rather than after Shopify's headings,
    so the non-commerce profiles are first-class rather than a commerce file with
    parts removed.
    """

    IDENTITY = "identity"
    PERSONAL_SHOPPER = "personal_shopper"
    COMMERCE_PROTOCOL = "commerce_protocol"
    AGENT_FLOW = "agent_flow"
    READ_ONLY = "read_only"
    SEARCH_AND_LISTINGS = "search_and_listings"
    ATTRIBUTION = "attribution"
    API_ACCESS = "api_access"
    CONTACT = "contact"
    POLICIES = "policies"
    RATE_LIMITS = "rate_limits"
    NOT_SUPPORTED = "not_supported"


# Which sections each profile may carry, in render order.
#
# "May", not "will": a section still has to earn its place with verified evidence.
# This table decides what is *appropriate* for a site shape; the probe decides what
# is *true*. Both have to agree before anything is written.
PROFILE_SECTIONS: dict[str, tuple[Section, ...]] = {
    # Shops: the full Shopify-shaped file, and the only profiles that may describe
    # a transaction at all.
    PATTERN_ECOMMERCE_RETAIL: (
        Section.IDENTITY,
        Section.PERSONAL_SHOPPER,
        Section.COMMERCE_PROTOCOL,
        Section.AGENT_FLOW,
        Section.READ_ONLY,
        Section.POLICIES,
        Section.RATE_LIMITS,
        Section.NOT_SUPPORTED,
    ),
    PATTERN_ECOMMERCE: (
        Section.IDENTITY,
        Section.PERSONAL_SHOPPER,
        Section.COMMERCE_PROTOCOL,
        Section.AGENT_FLOW,
        Section.READ_ONLY,
        Section.POLICIES,
        Section.RATE_LIMITS,
        Section.NOT_SUPPORTED,
    ),
    # Marketplaces and publishers: agents come to search and to cite, not to buy.
    # Attribution matters more here than anywhere else -- this is the shape where
    # content is lifted wholesale and the site gets nothing back.
    PATTERN_PUBLISHER: (
        Section.IDENTITY,
        Section.SEARCH_AND_LISTINGS,
        Section.READ_ONLY,
        Section.ATTRIBUTION,
        Section.RATE_LIMITS,
        Section.POLICIES,
        Section.NOT_SUPPORTED,
    ),
    # Service businesses: an agent's useful action is to find out what is offered
    # and how to make contact. There is nothing to transact and saying so is the
    # point.
    PATTERN_AGENCY: (
        Section.IDENTITY,
        Section.READ_ONLY,
        Section.CONTACT,
        Section.POLICIES,
        Section.NOT_SUPPORTED,
    ),
    PATTERN_PROFESSIONAL: (
        Section.IDENTITY,
        Section.READ_ONLY,
        Section.CONTACT,
        Section.POLICIES,
        Section.NOT_SUPPORTED,
    ),
    PATTERN_LOCAL: (
        Section.IDENTITY,
        Section.READ_ONLY,
        Section.CONTACT,
        Section.POLICIES,
        Section.NOT_SUPPORTED,
    ),
    PATTERN_SAAS: (
        Section.IDENTITY,
        Section.API_ACCESS,
        Section.READ_ONLY,
        Section.RATE_LIMITS,
        Section.CONTACT,
        Section.POLICIES,
        Section.NOT_SUPPORTED,
    ),
    # Documentation shapes: read it, cite it, do not hammer it.
    PATTERN_CATALOG: (
        Section.IDENTITY,
        Section.API_ACCESS,
        Section.READ_ONLY,
        Section.RATE_LIMITS,
        Section.NOT_SUPPORTED,
    ),
    PATTERN_WORKFLOW: (
        Section.IDENTITY,
        Section.READ_ONLY,
        Section.RATE_LIMITS,
        Section.NOT_SUPPORTED,
    ),
    PATTERN_INDEX_EXPORT: (
        Section.IDENTITY,
        Section.READ_ONLY,
        Section.ATTRIBUTION,
        Section.RATE_LIMITS,
        Section.NOT_SUPPORTED,
    ),
}

# The only profiles allowed to describe a transaction. Checked separately from the
# section table so the rule is stated once and can be asserted directly.
TRANSACTIONAL_PROFILES = frozenset({PATTERN_ECOMMERCE_RETAIL, PATTERN_ECOMMERCE})

# Sections that describe acting on behalf of a buyer. Refused outright on a
# non-transactional profile even if a probe somehow found an endpoint, because a
# law firm with a stray UCP document is a misconfiguration, not a shop.
TRANSACTIONAL_SECTIONS = frozenset(
    {Section.PERSONAL_SHOPPER, Section.COMMERCE_PROTOCOL, Section.AGENT_FLOW}
)


def transacts(profile: str) -> bool:
    return profile in TRANSACTIONAL_PROFILES


@dataclass(frozen=True, slots=True)
class Capability:
    """Something an agent may do, and the evidence that it can.

    `evidence` is not decoration. It is what the report shows the operator, and
    what makes a claim auditable months later when a client asks where a line in
    their file came from.
    """

    label: str
    url: str
    evidence: str
    transport: str = ""


@dataclass(frozen=True, slots=True)
class PolicyLink:
    """A published policy an agent is expected to respect."""

    label: str
    url: str


@dataclass(frozen=True, slots=True)
class OmittedSection:
    """A section that was appropriate for the profile and left out anyway.

    Recorded so the absence is legible. Without this a file missing its commerce
    section looks identical whether the site has no UCP endpoint or the probe
    timed out, and those need different responses from the operator.
    """

    section: Section
    reason: str


@dataclass(slots=True)
class AgentsDoc:
    """A complete, verified `agents.md`, ready to render."""

    site_url: str
    site_name: str = ""
    profile: str = PATTERN_AGENCY
    # Written by the LLM. Prose only -- it never supplies a URL or an endpoint.
    summary: str = ""
    agent_guidance: str = ""

    capabilities: list[Capability] = field(default_factory=list)
    policies: list[PolicyLink] = field(default_factory=list)
    read_only_urls: list[Capability] = field(default_factory=list)
    rate_limit_note: str = ""
    contact_url: str = ""
    llms_txt_url: str = ""

    ucp_version: str = ""
    ucp_supported: tuple[str, ...] = ()
    platform: str = "unknown"

    sections: tuple[Section, ...] = ()
    omitted: list[OmittedSection] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def transactional(self) -> bool:
        return transacts(self.profile)

    @property
    def claimed_urls(self) -> list[str]:
        """Every URL the rendered file will contain.

        The validator walks this: a URL here that no probe verified is the defect
        the whole module exists to prevent, and it is cheaper to assert on a list
        than to re-parse the rendered markdown.
        """
        urls: list[str] = []
        # Derived from the sections that survived, not from every field that
        # happens to be populated. A URL whose section was dropped is not claimed,
        # and listing it here would have the validator check something the file
        # never says -- or worse, let a leak look legitimate.
        if any(self.has(s) for s in TRANSACTIONAL_SECTIONS):
            urls += [c.url for c in self.capabilities]
        if self.has(Section.READ_ONLY) or self.has(Section.SEARCH_AND_LISTINGS):
            urls += [c.url for c in self.read_only_urls]
        if self.has(Section.API_ACCESS):
            urls += [c.url for c in self.read_only_urls]
            if self.llms_txt_url:
                urls.append(self.llms_txt_url)
        if self.has(Section.POLICIES):
            urls += [p.url for p in self.policies]
        if self.has(Section.CONTACT) and self.contact_url:
            urls.append(self.contact_url)
        return list(dict.fromkeys(u for u in urls if u))

    def has(self, section: Section) -> bool:
        return section in self.sections


def build_agents_doc(
    probe: ProbeResult,
    profile: str,
    site_name: str = "",
    policies: list[PolicyLink] | None = None,
    read_only: list[Capability] | None = None,
    contact_url: str = "",
    rate_limit_note: str = "",
) -> AgentsDoc:
    """Assemble the document from verified evidence and a site shape.

    Sections survive only where the profile permits them *and* something verified
    fills them. That conjunction is the design: the profile table alone would let a
    shop with no UCP endpoint publish an empty commerce section, and the probe alone
    would let a law firm publish a checkout flow.
    """
    doc = AgentsDoc(
        site_url=probe.site_url,
        site_name=site_name,
        profile=profile,
        platform=probe.platform,
        policies=list(policies or []),
        read_only_urls=list(read_only or []),
        contact_url=contact_url,
        rate_limit_note=rate_limit_note,
        notes=list(probe.notes),
    )

    permitted = PROFILE_SECTIONS.get(profile, PROFILE_SECTIONS[PATTERN_AGENCY])

    # Gated on the profile, not merely on the probe. A services site with a stray
    # UCP document is a misconfiguration, not a shop, and ingesting its endpoints
    # here would put them in `claimed_urls` even though every commerce section was
    # dropped -- a claim surviving the removal of the section that justified it.
    # Measured: an agency profile built from a Shopify probe listed
    # `s.myshopify.com/api/ucp/mcp` among its claims while rendering no commerce
    # section at all.
    if transacts(profile) and probe.has_ucp and probe.ucp_profile:
        doc.ucp_version = probe.ucp_profile.version
        doc.ucp_supported = probe.ucp_profile.supported_versions
        for service in probe.ucp_profile.services:
            doc.capabilities.append(
                Capability(
                    label=service.name,
                    url=service.endpoint,
                    transport=service.transport,
                    evidence=f"declared at {probe.ucp.url} (UCP {service.version or 'unversioned'})",
                )
            )

    if probe.llms_txt and probe.llms_txt.usable:
        doc.llms_txt_url = probe.llms_txt.url

    kept: list[Section] = []
    for section in permitted:
        available, reason = _section_available(section, doc, probe)
        if available:
            kept.append(section)
        elif reason:
            doc.omitted.append(OmittedSection(section=section, reason=reason))

    doc.sections = tuple(kept)
    return doc


def _section_available(section: Section, doc: AgentsDoc, probe: ProbeResult) -> tuple[bool, str]:
    """Whether a section has verified content, and if not, why not."""
    match section:
        case Section.IDENTITY | Section.NOT_SUPPORTED:
            # Always present. Identity needs no external evidence, and the
            # not-supported list is most useful exactly when everything else is
            # missing -- a file that says only "this site does not transact" is a
            # complete and useful answer.
            return True, ""

        case Section.COMMERCE_PROTOCOL | Section.AGENT_FLOW | Section.PERSONAL_SHOPPER:
            if not doc.transactional:
                return False, "the site is not a shop, so no transaction is described"
            if not probe.has_ucp:
                return False, (
                    f"no UCP profile at {probe.site_url}/.well-known/ucp, so no commerce "
                    "endpoint can be named"
                )
            if not probe.verified_endpoints:
                return False, "the UCP profile declares no MCP endpoint"
            return True, ""

        case Section.READ_ONLY | Section.SEARCH_AND_LISTINGS:
            if not doc.read_only_urls:
                return False, "no read-only URLs were verified during the crawl"
            return True, ""

        case Section.API_ACCESS:
            if not doc.read_only_urls and not doc.llms_txt_url:
                return False, "no documentation or API surface was verified"
            return True, ""

        case Section.CONTACT:
            if not doc.contact_url:
                return False, "no contact page was found in the crawl"
            return True, ""

        case Section.POLICIES:
            if not doc.policies:
                return False, "no policy pages were found in the crawl"
            return True, ""

        case Section.RATE_LIMITS:
            if not doc.rate_limit_note:
                return False, "no rate-limit guidance was supplied in the brief"
            return True, ""

        case Section.ATTRIBUTION:
            return True, ""

    return False, ""


# Policy pages an agent is expected to respect, matched on URL path. Kept apart
# from `ranking.CONTACT_URL_KEYWORDS`, which answers a different question -- that
# set includes store locators and help centres, which are contact routes but not
# policies, and conflating the two would list a store finder as terms of service.
POLICY_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("privacy", "Privacy"),
    ("terms", "Terms"),
    ("refund", "Refunds"),
    ("returns", "Returns"),
    ("shipping", "Shipping"),
    ("delivery", "Delivery"),
    ("cookie", "Cookies"),
    ("accessibility", "Accessibility"),
    ("disclaimer", "Disclaimer"),
)


def links_from_pages(
    pages: list[tuple[str, str]], limit: int = 12
) -> tuple[list[Capability], list[PolicyLink], str]:
    """Split a completed crawl's pages into read-only links, policies and contact.

    `pages` is (url, title). Every URL here was fetched successfully during that
    run, which is what makes it citable: the same evidence rule as the probe, met
    by a different means.

    Returns the three in the shapes `build_agents_doc` expects. Ordering follows
    the input, so the caller's ranking decides what an agent sees first.
    """
    # The keyword sets directly rather than `ranking.is_contact_page`, which takes
    # a full `PageEntry`. Building one from a (url, title) pair just to ask a
    # keyword question would invent the twenty other fields it does not use.
    from app.core.ranking import CONTACT_TITLE_KEYWORDS, CONTACT_URL_KEYWORDS

    def looks_like_contact(url: str, title: str) -> bool:
        low_url, low_title = url.lower(), (title or "").lower()
        return any(k in low_url for k in CONTACT_URL_KEYWORDS) or any(
            k in low_title for k in CONTACT_TITLE_KEYWORDS
        )

    policies: list[PolicyLink] = []
    read_only: list[Capability] = []
    contact = ""
    seen_policies: set[str] = set()

    for url, title in pages:
        lowered = url.lower()
        matched = next((label for key, label in POLICY_KEYWORDS if key in lowered), "")
        if matched:
            if matched not in seen_policies:
                seen_policies.add(matched)
                policies.append(PolicyLink(matched, url))
            continue
        if not contact and looks_like_contact(url, title):
            contact = url
            continue
        if len(read_only) < limit:
            read_only.append(
                Capability(
                    label=title or url,
                    url=url,
                    evidence="fetched successfully during the site crawl",
                )
            )

    return read_only, policies, contact
