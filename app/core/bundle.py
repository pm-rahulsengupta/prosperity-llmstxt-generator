"""The complete set of agent-facing files for one site, per scenario.

Everything before this generated one file at a time. A site does not need one
file; it needs the set its scenario calls for, consistent with each other. An
`agents.md` pointing at an `llms.txt` that was never published, or an
`ai-catalog.json` naming an MCP server the `robots.txt` blocks, is worse than
either file alone — the inconsistency is what an agent trips over.

So the scenario decides the manifest. A local business needs robots rules,
llms.txt, agents.md and a Link header; a shop adds the commerce protocol; an API
site adds the catalog and the OpenAPI pointer. Files outside the scenario are
not generated and are named as deliberately absent, because "we did not make you
one" and "you do not need one" are different answers.

**Declared, then verified.** The onboarding lets an operator name an MCP server,
an A2A card or an OpenAPI description — things no probe can discover, because
advertising them is the very thing the catalog exists to do. A declared endpoint
is a candidate. It reaches a published file only after it answers with the right
content type, and is otherwise reported as declared-but-unreachable. That keeps
the rule that nothing unverified is claimed, rather than trading it away for the
feature.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum

import httpx

from app.core.onboarding import BotPolicy, SiteBrief

__all__ = [
    "SCENARIO_FILES",
    "Artifact",
    "Bundle",
    "DeploymentTask",
    "Effort",
    "build_bundle",
    "render_headers",
    "render_robots",
    "verify_declared",
]

# AI crawlers named in the checklist. Kept as data because the list changes
# monthly while the policy behind it does not.
SEARCH_BOTS = ("OAI-SearchBot", "PerplexityBot", "ClaudeBot", "Google-Extended")
TRAINING_BOTS = ("GPTBot", "CCBot", "anthropic-ai")


class Effort(StrEnum):
    """Who has to do the work, which decides who the item goes to.

    A generated file and a server change look alike on a checklist and are
    nothing alike in a client's week. Handing a marketing manager a list that
    mixes "upload this file" with "implement content negotiation at the edge"
    produces a list nobody starts, because the first blocked item stops it.
    """

    DROP_IN = "drop_in"
    SERVER_CONFIG = "server_config"
    CODE_CHANGE = "code_change"
    INFRASTRUCTURE = "infrastructure"


EFFORT_LABELS: dict[Effort, str] = {
    Effort.DROP_IN: "Upload the file",
    Effort.SERVER_CONFIG: "Server, CDN or host configuration",
    Effort.CODE_CHANGE: "Template or front-end change",
    Effort.INFRASTRUCTURE: "Stand up a service",
}

EFFORT_OWNERS: dict[Effort, str] = {
    Effort.DROP_IN: "Anyone with access to the web root or CMS",
    Effort.SERVER_CONFIG: "Whoever administers the CDN or web server",
    Effort.CODE_CHANGE: "The site's front-end developer",
    Effort.INFRASTRUCTURE: "A backend engineer; this is a build, not a setting",
}


@dataclass(frozen=True, slots=True)
class DeploymentTask:
    """One thing that has to happen for a component to be live.

    Carried separately from the artifacts because most of these produce no file
    at all. `Link` headers, Markdown negotiation and WebMCP cannot be handed over
    as a download; they are changes to how the site answers, and a bundle that
    only lists files silently drops the half of the checklist that needs a
    developer.
    """

    component: str
    effort: Effort
    what: str
    # Platform-specific instructions where the platform is known.
    platform_hint: str = ""
    blocked_by: str = ""


@dataclass(frozen=True, slots=True)
class Artifact:
    """One generated file, and where it goes."""

    name: str
    path: str
    body: str
    media_type: str
    note: str = ""


@dataclass(slots=True)
class DeclaredEndpoint:
    """Something the operator said exists, and whether it answered."""

    kind: str
    url: str
    verified: bool = False
    detail: str = ""


@dataclass(slots=True)
class Bundle:
    site_url: str
    scenario: str
    artifacts: list[Artifact] = field(default_factory=list)
    declared: list[DeclaredEndpoint] = field(default_factory=list)
    # Files this scenario does not call for, named so their absence is a decision
    # rather than an oversight.
    not_needed: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    tasks: list[DeploymentTask] = field(default_factory=list)

    @property
    def verified_endpoints(self) -> list[DeclaredEndpoint]:
        return [d for d in self.declared if d.verified]

    @property
    def unreachable(self) -> list[DeclaredEndpoint]:
        return [d for d in self.declared if not d.verified]

    def get(self, name: str) -> Artifact | None:
        return next((a for a in self.artifacts if a.name == name), None)

    def tasks_by_effort(self) -> dict[Effort, list[DeploymentTask]]:
        """Grouped so the file uploads can be done without waiting on a developer.

        Order matters: the drop-ins are the ones a client can action today, and
        putting them behind an infrastructure item is how a whole list stalls.
        """
        grouped: dict[Effort, list[DeploymentTask]] = {effort: [] for effort in Effort}
        for task in self.tasks:
            grouped[task.effort].append(task)
        return {effort: items for effort, items in grouped.items() if items}

    @property
    def developer_tasks(self) -> list[DeploymentTask]:
        """Everything that cannot be done by uploading a file."""
        return [t for t in self.tasks if t.effort is not Effort.DROP_IN]


# Which files each scenario calls for. Keyed on the operator's stated goal, which
# is the only input that describes intent rather than structure.
SCENARIO_FILES: dict[str, tuple[str, ...]] = {
    "contact_local_business": ("robots.txt", "llms.txt", "agents.md", "_headers"),
    "contact_agency": ("robots.txt", "llms.txt", "agents.md", "_headers"),
    "book_appointment": ("robots.txt", "llms.txt", "agents.md", "_headers"),
    "read_and_cite": ("robots.txt", "llms.txt", "llms-full.txt", "agents.md", "_headers"),
    "shop_on_store": ("robots.txt", "llms.txt", "agents.md", "_headers", "ai-catalog.json"),
    "find_local_inventory": (
        "robots.txt",
        "llms.txt",
        "agents.md",
        "_headers",
        "ai-catalog.json",
    ),
    "use_the_api": (
        "robots.txt",
        "llms.txt",
        "agents.md",
        "_headers",
        "ai-catalog.json",
    ),
}

ALL_FILES = ("robots.txt", "llms.txt", "llms-full.txt", "agents.md", "_headers", "ai-catalog.json")

# Content type expected of each declared endpoint kind.
DECLARED_EXPECT = {
    "mcp": ("application/json",),
    "a2a": ("application/json", "application/a2a-agent-card+json"),
    "openapi": ("application/json", "application/yaml", "text/yaml"),
}


async def verify_declared(
    brief: SiteBrief, user_agent: str, timeout: float = 15.0
) -> list[DeclaredEndpoint]:
    """Check that each declared endpoint actually answers.

    An operator naming their own MCP server is the only way we learn about it,
    and it is also the easiest place for a typo or a decommissioned host to enter
    a published file. Verification is what makes the declaration safe to act on.
    """
    candidates = [
        *(DeclaredEndpoint("mcp", url) for url in brief.mcp_server_url),
        *(DeclaredEndpoint("a2a", url) for url in brief.a2a_agent_url),
        *(DeclaredEndpoint("openapi", url) for url in brief.openapi_url),
    ]
    if not candidates:
        return []

    async with httpx.AsyncClient(
        follow_redirects=True, timeout=timeout, headers={"User-Agent": user_agent}
    ) as client:

        async def check(endpoint: DeclaredEndpoint) -> DeclaredEndpoint:
            try:
                response = await client.get(endpoint.url)
            except httpx.HTTPError as exc:
                endpoint.detail = f"unreachable ({type(exc).__name__})"
                return endpoint
            content_type = (response.headers.get("content-type") or "").split(";")[0].lower()
            if response.status_code >= 400:
                endpoint.detail = f"answered {response.status_code}"
            elif "html" in content_type:
                # An MCP server that returns a web page is a web page.
                endpoint.detail = f"answered {response.status_code} with HTML, not an endpoint"
            elif content_type and not any(
                e in content_type for e in DECLARED_EXPECT.get(endpoint.kind, ())
            ):
                endpoint.detail = f"served as {content_type}"
            else:
                endpoint.verified = True
                endpoint.detail = f"{response.status_code} {content_type or 'no content type'}"
            return endpoint

        return list(await asyncio.gather(*(check(c) for c in candidates)))


def render_robots(brief: SiteBrief, sitemap_url: str = "") -> str:
    """robots.txt rules and the Content-Signal line, from the stated policy.

    Written as an addition rather than a replacement. A client's robots.txt
    already carries rules we did not write and cannot see the reasons for, and
    handing them a file that silently drops those is how a tool takes a site out
    of Google.
    """
    policy = brief.ai_bot_policy
    lines = ["# AI crawler rules — add these to your existing robots.txt.", ""]

    if policy is BotPolicy.BLOCK_ALL:
        for bot in SEARCH_BOTS + TRAINING_BOTS:
            lines += [f"User-agent: {bot}", "Disallow: /", ""]
        signal = "ai-train=no, search=no, ai-input=no"
    elif policy is BotPolicy.ALLOW_SEARCH_ONLY:
        for bot in SEARCH_BOTS:
            lines += [f"User-agent: {bot}", "Allow: /", ""]
        for bot in TRAINING_BOTS:
            lines += [f"User-agent: {bot}", "Disallow: /", ""]
        signal = "ai-train=no, search=yes, ai-input=yes"
    else:
        for bot in SEARCH_BOTS + TRAINING_BOTS:
            lines += [f"User-agent: {bot}", "Allow: /", ""]
        signal = "ai-train=yes, search=yes, ai-input=yes"

    lines += ["User-agent: *", "Allow: /", f"Content-Signal: {signal}", ""]
    if sitemap_url:
        lines += [f"Sitemap: {sitemap_url}", ""]

    if policy is BotPolicy.ALLOW_SEARCH_ONLY:
        lines += [
            "# Note: on Cloudflare, blocking Training also blocks Googlebot, Bingbot",
            "# and Applebot from 15 September 2026 — multi-purpose crawlers are judged",
            "# by their strictest rule. Check your CDN's AI crawler settings as well;",
            "# these lines alone will not produce the outcome above.",
        ]
    return "\n".join(lines) + "\n"


def render_headers(site_url: str, has_llms: bool, has_catalog: bool, openapi_url: str = "") -> str:
    """A Cloudflare `_headers` file advertising the agent surfaces via Link rels.

    Only rels for files that will exist. A `Link` header pointing at a 404 is
    worse than no header: it costs an agent a request and teaches it to distrust
    the rest.
    """
    lines = ["# Cloudflare Pages _headers — agent-aware Link rels.", "", "/*"]
    lines.append('  Link: </sitemap.xml>; rel="sitemap"')
    if has_llms:
        lines.append('  Link: </llms.txt>; rel="describedby"; type="text/markdown"')
    if has_catalog:
        lines.append('  Link: </.well-known/ai-catalog.json>; rel="ai-catalog"')
    if openapi_url:
        lines.append(
            f'  Link: <{openapi_url}>; rel="service-desc"; type="application/vnd.oai.openapi+json"'
        )
    return "\n".join(lines) + "\n"


# Host-specific instructions for the one task that has no portable answer. Where
# the platform is unknown the generic wording is used, because a wrong hint costs
# more than an unspecific one -- a developer following instructions for the wrong
# CDN loses an afternoon before finding out.
HEADER_HINTS: dict[str, str] = {
    "shopify": (
        "Shopify does not expose response headers. Serve the Link header from a "
        "reverse proxy in front of the store, or skip this item."
    ),
    "wordpress": (
        "Add to the server config rather than a plugin: `Header add Link` in "
        "Apache, or `add_header Link` in nginx."
    ),
    "nextjs": "Set it in `headers()` in next.config.js, or in a Cloudflare `_headers` file.",
    "wix": "Wix does not allow custom response headers. This item is not achievable there.",
    "squarespace": "Squarespace does not allow custom response headers on the root domain.",
}


def _deployment_tasks(bundle: Bundle, brief: SiteBrief, platform: str) -> list[DeploymentTask]:
    """The work that produces no downloadable file.

    Half the checklist lives here. `Link` headers, Markdown negotiation, WebMCP
    and the accessibility items are changes to how a site answers or how it is
    built, and a bundle that listed only files would hand a client the easy half
    and quietly drop the rest.
    """
    tasks: list[DeploymentTask] = []

    for artifact in bundle.artifacts:
        if artifact.name == "_headers":
            continue
        tasks.append(
            DeploymentTask(
                component=artifact.name,
                effort=Effort.DROP_IN,
                what=f"Serve this at {artifact.path} as {artifact.media_type}.",
                platform_hint=artifact.note,
            )
        )

    if bundle.get("_headers") is not None:
        tasks.append(
            DeploymentTask(
                component="Link headers",
                effort=Effort.SERVER_CONFIG,
                what=(
                    "Advertise the agent surfaces in the Link response header on every "
                    "page. The generated `_headers` file covers Cloudflare Pages."
                ),
                platform_hint=HEADER_HINTS.get(
                    platform,
                    "Set the same Link headers wherever your host allows response headers.",
                ),
            )
        )

    tasks.append(
        DeploymentTask(
            component="Markdown negotiation",
            effort=Effort.SERVER_CONFIG,
            what=(
                "Return markdown when the request carries `Accept: text/markdown`. This "
                "is the one item with measured benefit rather than assumed: roughly a "
                "five-fold drop in payload for agents already fetching the site."
            ),
            platform_hint=(
                "Cloudflare offers it as a transform. Otherwise it is a content-negotiation "
                "branch in the application."
            ),
        )
    )

    tasks.append(
        DeploymentTask(
            component="Layer 1 page checks",
            effort=Effort.CODE_CHANGE,
            what=(
                "Semantic HTML, roles on clickable divs, form labels, 24px tap targets, "
                "layout stability and no ghost overlays. Agents walking the accessibility "
                "tree depend on these, and no generated file substitutes for them."
            ),
            platform_hint="Verify with `npx lighthouse <site> --view` and an accessibility tree walk.",
        )
    )

    if brief.mcp_server_url and not any(d.kind == "mcp" and d.verified for d in bundle.declared):
        tasks.append(
            DeploymentTask(
                component="MCP server",
                effort=Effort.INFRASTRUCTURE,
                what="An MCP server was declared but did not answer, so nothing references it.",
                blocked_by=next(
                    (d.detail for d in bundle.declared if d.kind == "mcp"), "not reachable"
                ),
            )
        )

    if brief.ai_bot_policy is BotPolicy.ALLOW_SEARCH_ONLY:
        tasks.append(
            DeploymentTask(
                component="CDN AI crawler settings",
                effort=Effort.SERVER_CONFIG,
                what=(
                    "robots.txt alone will not produce the stated policy. Check the CDN's "
                    "own AI crawler controls, which override it."
                ),
                platform_hint=(
                    "On Cloudflare, blocking Training also blocks Googlebot, Bingbot and "
                    "Applebot from 15 September 2026 — multi-purpose crawlers are judged by "
                    "their strictest rule. Confirm this is intended before enabling it."
                ),
            )
        )

    return tasks


def build_bundle(
    site_url: str,
    brief: SiteBrief,
    declared: list[DeclaredEndpoint] | None = None,
    llms_txt: str = "",
    llms_full: str = "",
    agents_md: str = "",
    ai_catalog: str = "",
    sitemap_url: str = "",
    platform: str = "",
    generated_on: date | None = None,
) -> Bundle:
    """Assemble every file this scenario calls for, and name the ones it does not."""
    scenario = brief.primary_action.value or "contact_agency"
    wanted = SCENARIO_FILES.get(scenario, SCENARIO_FILES["contact_agency"])

    bundle = Bundle(site_url=site_url, scenario=scenario, declared=list(declared or []))

    openapi = next((d.url for d in bundle.verified_endpoints if d.kind == "openapi"), "")

    if "robots.txt" in wanted:
        bundle.artifacts.append(
            Artifact(
                "robots.txt",
                "/robots.txt",
                render_robots(brief, sitemap_url),
                "text/plain",
                note="Merge with the existing file; do not replace it.",
            )
        )
    if "llms.txt" in wanted and llms_txt:
        bundle.artifacts.append(Artifact("llms.txt", "/llms.txt", llms_txt, "text/markdown"))
    if "llms-full.txt" in wanted and llms_full:
        bundle.artifacts.append(
            Artifact("llms-full.txt", "/llms-full.txt", llms_full, "text/markdown")
        )
    if "agents.md" in wanted and agents_md:
        bundle.artifacts.append(Artifact("agents.md", "/agents.md", agents_md, "text/markdown"))
    if "ai-catalog.json" in wanted and ai_catalog:
        bundle.artifacts.append(
            Artifact(
                "ai-catalog.json",
                "/.well-known/ai-catalog.json",
                ai_catalog,
                "application/ai-catalog+json",
            )
        )
    if "_headers" in wanted:
        bundle.artifacts.append(
            Artifact(
                "_headers",
                "_headers",
                render_headers(
                    site_url,
                    has_llms=bool(llms_txt),
                    has_catalog=bool(ai_catalog) and "ai-catalog.json" in wanted,
                    openapi_url=openapi,
                ),
                "text/plain",
                note="Cloudflare Pages. On other hosts, set the same Link headers at the edge.",
            )
        )

    bundle.tasks = _deployment_tasks(bundle, brief, platform)

    produced = {a.name for a in bundle.artifacts}
    bundle.not_needed = [name for name in ALL_FILES if name not in wanted and name not in produced]

    for name in wanted:
        if name not in produced:
            bundle.notes.append(
                f"{name} is called for by this scenario but was not generated — "
                "run the llms.txt generator for this site first."
            )

    for endpoint in bundle.unreachable:
        bundle.notes.append(
            f"Declared {endpoint.kind.upper()} endpoint {endpoint.url} was not published: "
            f"{endpoint.detail}."
        )
    return bundle
