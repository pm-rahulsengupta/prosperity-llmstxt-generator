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

import httpx

from app.core.components import (
    COMPONENTS,
    EFFORT_LABELS,
    EFFORT_OWNERS,
    GENERIC_HEADER_HINT,
    HEADER_HINTS,
    Effort,
    SiteType,
    by_key,
    for_developer,
)
from app.core.onboarding import BotPolicy, SiteBrief

# Re-exported, not redefined. `bundle` used to declare its own `Effort` with the
# same members, and because both were StrEnum the cross-module dictionary lookup
# in the handover page hashed identically and worked -- by accident. Two classes,
# `is` comparison false, one `KeyError` away from a broken page the moment either
# stopped being a StrEnum or a member value changed. The identity is asserted in
# the tests now rather than left to coincidence.
__all__ = [
    "EFFORT_LABELS",
    "EFFORT_OWNERS",
    "SCENARIO_COMPONENTS",
    "Artifact",
    "Bundle",
    "DeploymentTask",
    "Effort",
    "build_bundle",
    "render_headers",
    "render_robots",
    "scenario_files",
    "verify_declared",
]

# AI crawlers named in the checklist. Kept as data because the list changes
# monthly while the policy behind it does not.
SEARCH_BOTS = ("OAI-SearchBot", "PerplexityBot", "ClaudeBot", "Google-Extended")
TRAINING_BOTS = ("GPTBot", "CCBot", "anthropic-ai")


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


# Which components each scenario calls for. Keyed on the operator's stated goal,
# because which files a *goal* needs is a product decision rather than a property
# of any component -- this is the one table that is genuinely not derivable.
#
# Keyed on component keys rather than filenames, so a typo fails a test instead
# of silently naming a file nothing produces. The filenames come from the
# registry, which is the only place they are written down.
# The goals that involve money changing hands, which decides whether the
# developer list includes the commerce components at all.
TRANSACTIONAL_SCENARIOS = frozenset({"shop_on_store", "find_local_inventory"})

SCENARIO_COMPONENTS: dict[str, tuple[str, ...]] = {
    "contact_local_business": ("robots", "llms-txt", "agents-md", "link-header"),
    "contact_agency": ("robots", "llms-txt", "agents-md", "link-header"),
    "book_appointment": ("robots", "llms-txt", "agents-md", "link-header"),
    "read_and_cite": ("robots", "llms-txt", "llms-full", "agents-md", "link-header"),
    "shop_on_store": ("robots", "llms-txt", "agents-md", "link-header", "ai-catalog"),
    "find_local_inventory": ("robots", "llms-txt", "agents-md", "link-header", "ai-catalog"),
    "use_the_api": ("robots", "llms-txt", "agents-md", "link-header", "ai-catalog"),
}


def scenario_files(scenario: str) -> tuple[str, ...]:
    """The filenames a scenario calls for, resolved through the registry."""
    keys = SCENARIO_COMPONENTS.get(scenario, SCENARIO_COMPONENTS["contact_agency"])
    return tuple(
        component.artifact
        for key in keys
        if (component := by_key(key)) is not None and component.artifact
    )


ALL_FILES = tuple(dict.fromkeys(c.artifact for c in COMPONENTS if c.artifact))

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
def _deployment_tasks(bundle: Bundle, brief: SiteBrief, platform: str) -> list[DeploymentTask]:
    """The work that produces no downloadable file, projected from the registry.

    This began as a hand-written list that happened to agree with the checklist,
    which is the arrangement that drifts: adding a component wired it into the
    audit and left the handover silent about it. Every developer task is now
    derived from `for_developer()`, so the two cannot disagree.

    The uploads are still enumerated from the bundle's own artefacts, because
    which files were actually produced is a fact about this run rather than about
    the registry.
    """
    tasks: list[DeploymentTask] = []

    for artifact in bundle.artifacts:
        tasks.append(
            DeploymentTask(
                component=artifact.name,
                effort=Effort.DROP_IN,
                what=f"Serve this at {artifact.path} as {artifact.media_type}.",
                platform_hint=artifact.note,
            )
        )

    site_type = (
        SiteType.ECOMMERCE if bundle.scenario in TRANSACTIONAL_SCENARIOS else SiteType.CONTENT
    )
    for component in for_developer(site_type):
        hint = ""
        blocked = ""
        if component.key == "link-header":
            hint = HEADER_HINTS.get(platform, GENERIC_HEADER_HINT)
        elif component.key == "markdown-negotiation":
            hint = (
                "Cloudflare offers it as a transform. Otherwise it is a "
                "content-negotiation branch in the application."
            )
        elif component.key == "web-bot-auth" and brief.ai_bot_policy is BotPolicy.ALLOW_SEARCH_ONLY:
            hint = (
                "On Cloudflare, blocking Training also blocks Googlebot, Bingbot and "
                "Applebot from 15 September 2026 -- multi-purpose crawlers are judged "
                "by their strictest rule. Confirm this is intended before enabling it."
            )
        elif component.key == "mcp-card" and brief.mcp_server_url:
            declared = next((d for d in bundle.declared if d.kind == "mcp"), None)
            if declared is not None and not declared.verified:
                blocked = declared.detail or "not reachable"

        tasks.append(
            DeploymentTask(
                component=component.title,
                effort=component.effort,
                what=component.why or component.verify,
                platform_hint=hint or component.verify,
                blocked_by=blocked,
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
    wanted = scenario_files(scenario)

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
    # Offered whenever one exists, not only where the scenario names it. The
    # pipeline generates llms-full for every run -- `GenerateOptions.generate_full`
    # defaults to True and nothing was setting it -- so gating delivery on the
    # scenario meant paying to build the file for all seven goals and surfacing it
    # for one. A file we produced and hid is worse than one we never made: it is
    # unreviewed, unjudged, and still sitting in the database.
    #
    # The scenario table still decides what a goal *calls for*; it no longer
    # decides what an operator is allowed to see.
    if llms_full:
        bundle.artifacts.append(
            Artifact(
                "llms-full.txt",
                "/llms-full.txt",
                llms_full,
                "text/markdown",
                note=(
                    ""
                    if "llms-full.txt" in wanted
                    else "Generated, though this goal does not require it. Publish only "
                    "if you want agents to read the whole corpus."
                ),
            )
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
                "run a crawl for this site first."
            )

    for endpoint in bundle.unreachable:
        bundle.notes.append(
            f"Declared {endpoint.kind.upper()} endpoint {endpoint.url} was not published: "
            f"{endpoint.detail}."
        )
    return bundle
