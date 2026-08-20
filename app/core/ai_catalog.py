"""`/.well-known/ai-catalog.json` — Agentic Resource Discovery.

The third agent-facing file, and the only one of the three with an actual
specification behind it. Google published ARD on 17 June 2026 with Cisco,
Databricks, GitHub, GoDaddy, Hugging Face, Microsoft, Nvidia, Salesforce,
ServiceNow and Snowflake, under Apache 2.0, building on the Linux Foundation's AI
Catalog data model. Where `llms.txt` lists content and `agents.md` describes
behaviour in prose, this is a machine-readable index of *capabilities*: MCP
servers, A2A agents, OpenAPI tools, and nested catalogs.

Two facts worth stating plainly before anyone treats it as settled.

**It is a v0.9 draft and adoption is near zero.** Measured 2026-08-20:
`vercel.com` and `suganthan.com` serve one; `allbirds.com` and
`prosperitymedia.com.au` return 404. Publishing one today is being early, not
being compliant, and the file says so in its own documentation link rather than
implying a maturity it does not have.

**Most sites have nothing to put in it.** The entry types are capabilities — an
MCP server, an agent card, an API description. A brochure site has none of those,
and a catalog listing only a sitemap is noise wearing a standard's clothes. So
this generates a file only when there is something real to list, and says so
otherwise. That is the same rule the agents.md generator runs on, applied to a
format whose whole purpose is to be trusted by machines.

Shape taken from the two live files rather than from prose: `specVersion`, a
`host` block with `displayName`/`identifier`/`documentationUrl`, and `entries`
each carrying a URN `identifier`, `displayName`, media `type`, `url`,
`description` and `tags`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date

from app.scrape.agents_probe import ProbeResult
from app.scrape.tech_probe import TechProfile

__all__ = ["AiCatalog", "CatalogEntry", "build_catalog", "render_catalog"]

SPEC_VERSION = "1.0"
CONTENT_TYPE = "application/ai-catalog+json"
SPEC_URL = (
    "https://developers.googleblog.com/announcing-the-agentic-resource-discovery-specification/"
)

# Media types the two live catalogs use for the surfaces we can verify.
TYPE_MARKDOWN = "text/markdown"
TYPE_JSON = "application/json"
TYPE_XML = "application/xml"
TYPE_MCP = "application/json"
TYPE_A2A = "application/a2a-agent-card+json"

_SLUG = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    """One capability, and the evidence it exists."""

    identifier: str
    display_name: str
    media_type: str
    url: str
    description: str
    tags: tuple[str, ...] = ()
    evidence: str = ""

    def to_dict(self) -> dict:
        entry = {
            "identifier": self.identifier,
            "displayName": self.display_name,
            "type": self.media_type,
            "url": self.url,
            "description": self.description,
        }
        if self.tags:
            entry["tags"] = list(self.tags)
        return entry


@dataclass(slots=True)
class AiCatalog:
    host_name: str
    host_identifier: str
    entries: list[CatalogEntry] = field(default_factory=list)
    documentation_url: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def worth_publishing(self) -> bool:
        """Whether this catalog says anything a catalog is for.

        A file listing nothing but a sitemap is noise wearing a standard's
        clothes: every crawler already finds a sitemap from robots.txt, and
        publishing an near-empty catalog invites an agent to spend a request
        learning nothing. Being early is defensible; being early and empty is not.
        """
        return len(self.entries) >= 2

    def to_dict(self) -> dict:
        host = {"displayName": self.host_name, "identifier": self.host_identifier}
        if self.documentation_url:
            host["documentationUrl"] = self.documentation_url
        return {
            "specVersion": SPEC_VERSION,
            "host": host,
            "entries": [entry.to_dict() for entry in self.entries],
        }


def _urn(domain: str, kind: str, name: str) -> str:
    """`urn:air:<domain>:<kind>:<slug>`, as both live catalogs form them."""
    slug = _SLUG.sub("-", name.lower()).strip("-") or "resource"
    return f"urn:air:{domain}:{kind}:{slug}"


def build_catalog(
    probe: ProbeResult,
    tech: TechProfile | None = None,
    site_name: str = "",
    agents_md_url: str = "",
) -> AiCatalog:
    """Assemble a catalog from verified surfaces only.

    Every entry traces to a probe that answered with the right content type. The
    same rule as the agents.md generator, and it matters more here: this file is
    parsed by machines that will connect to whatever it lists, so a wrong entry is
    not a misleading sentence but a failed connection attempt.
    """
    domain = probe.site_url.split("//")[-1].strip("/").removeprefix("www.")
    catalog = AiCatalog(
        host_name=site_name or domain,
        host_identifier=probe.site_url,
        documentation_url=agents_md_url or (probe.agents_md.url if probe.has_agents_md else ""),
    )

    if probe.has_ucp and probe.ucp_profile:
        for service in probe.ucp_profile.services:
            if not service.endpoint:
                continue
            transport = (service.transport or "endpoint").lower()
            catalog.entries.append(
                CatalogEntry(
                    identifier=_urn(domain, transport, service.name),
                    display_name=f"{service.name} ({transport.upper()})",
                    media_type=TYPE_MCP if transport == "mcp" else TYPE_JSON,
                    url=service.endpoint,
                    description=(
                        f"Universal Commerce Protocol {service.name} capability over "
                        f"{transport.upper()}, declared at {probe.ucp.url}."
                    ),
                    tags=("ucp", "commerce", transport),
                    evidence=f"declared in {probe.ucp.url}",
                )
            )

    if probe.has_agents_md:
        catalog.entries.append(
            CatalogEntry(
                identifier=_urn(domain, "docs", "agents-md"),
                display_name="Agent instructions",
                media_type=TYPE_MARKDOWN,
                url=probe.agents_md.url,
                description="How agents should behave on this site, and what it does not support.",
                tags=("agents", "instructions"),
                evidence=f"fetched {probe.agents_md.content_type}",
            )
        )

    if probe.llms_txt and probe.llms_txt.usable:
        catalog.entries.append(
            CatalogEntry(
                identifier=_urn(domain, "docs", "llms-txt"),
                display_name="Content index for language models",
                media_type=TYPE_MARKDOWN,
                url=probe.llms_txt.url,
                description="Curated index of the site's most useful pages.",
                tags=("llms", "content", "index"),
                evidence=f"fetched {probe.llms_txt.content_type}",
            )
        )

    for detection in tech.endpoints if tech else []:
        media = TYPE_XML if "xml" in detection.evidence else TYPE_JSON
        # Sitemaps are excluded deliberately: robots.txt already advertises them
        # and every crawler reads that first, so listing one here spends an agent's
        # request to tell it something it knows.
        if "sitemap" in detection.name.lower():
            continue
        catalog.entries.append(
            CatalogEntry(
                identifier=_urn(domain, "api", detection.name),
                display_name=detection.name,
                media_type=media,
                url=detection.url,
                description=f"{detection.name} exposed by this site.",
                tags=("api", "read-only"),
                evidence=f"answered {detection.evidence}",
            )
        )

    if not catalog.worth_publishing:
        catalog.notes.append(
            "Not enough verified capabilities to justify a catalog. Agentic Resource "
            "Discovery indexes MCP servers, agent cards and APIs; a file listing one "
            "document is noise, and every crawler already finds a sitemap from "
            "robots.txt. Publish llms.txt and agents.md first."
        )

    catalog.notes.append(
        "Agentic Resource Discovery is a v0.9 draft published 17 June 2026 and "
        "adoption is near zero. Publishing this is being early, not being compliant."
    )
    return catalog


def render_catalog(catalog: AiCatalog, generated_on: date | None = None) -> str:
    """Serialise. Deterministic key order and a trailing newline, like the rest."""
    document = catalog.to_dict()
    # Not part of the spec, and namespaced so it cannot collide with one. A file
    # nobody can date is a file nobody can tell is stale, and this convention is
    # eight weeks old.
    document["x-generated"] = {
        "by": "Prosperity llms.txt generator",
        "on": (generated_on or date.today()).isoformat(),
        "spec": SPEC_URL,
    }
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"
