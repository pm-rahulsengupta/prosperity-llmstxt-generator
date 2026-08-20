"""One registry for the twenty-one agent-readiness components.

Before this, the same twenty-one things were modelled three times and linked
nowhere. `readiness.CHECKLIST` knew each component's priority, applicability and
verify command but nothing about producing it. `bundle.SCENARIO_FILES` knew which
files a scenario needed but not what proved them. `bundle._deployment_tasks` knew
who did the work but not which check it satisfied. `robots.txt` appeared in all
three under three different names, and a component added to one was invisible to
the other two.

Everything downstream is now a projection of this module: the readiness audit,
the generated bundle, the deployment handover, the family tabs, and both action
checklists. Four consumers, one derivation, and a component that is added here
cannot be missing from any of them.

This is deliberately the lowest layer. It imports nothing from `app.core` or
`app.scrape`, which is what lets `readiness` and `bundle` both depend on it
without either depending on the other. The types they used to own live here for
the same reason.

## The four states

A component's state is derived per request and never stored, so the answer is
always about the site as it is now rather than as it was when someone last looked:

* **LIVE** — the probe found it published, with the right content type.
* **READY** — generated and waiting to be uploaded.
* **TEMPLATE** — a starting point exists but asserts something unverified. Never
  publishable; see below.
* **NOT_APPLICABLE** — this site type or scenario does not call for it.

## Why a template is not an artefact

Four components describe services rather than documents: an MCP server card, an
A2A agent card, a WebMCP registration, OAuth resource metadata. Generating one
means asserting a service exists, which is the single thing this tool refuses to
do -- an agent reading an invented endpoint tries it, fails, and reports the
client's site as broken.

So a template is scaffolding for a developer, not a draft of a publishable file.
It carries obvious placeholders, it is excluded from every bundle and download,
and nothing references it. When the operator declares the real endpoint and
`verify_declared` confirms it answers, the same component produces a genuine
artefact and the template disappears. That transition is the design; the template
is what makes the work legible in the meantime.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "COMPONENTS",
    "Applicability",
    "Component",
    "ComponentState",
    "Effort",
    "Family",
    "Priority",
    "SiteType",
    "by_family",
    "by_key",
]


class Priority(StrEnum):
    MUST = "Must"
    SHOULD = "Should"
    OPTIONAL = "Optional"


class SiteType(StrEnum):
    CONTENT = "content"
    APP_API = "app_api"
    ECOMMERCE = "ecommerce"


class Applicability(StrEnum):
    YES = "yes"
    # The sheet's "if you run one" / "if you have a public API". Real, common,
    # and not something a probe can settle -- so absence is reported, not failed.
    CONDITIONAL = "conditional"
    NO = "no"


class Effort(StrEnum):
    """Who has to do the work, which decides who the item goes to.

    A generated file and a server change look alike on a checklist and are
    nothing alike in a client's week. Mixing "upload this file" with "implement
    content negotiation at the edge" produces a list nobody starts, because the
    first blocked item stops it.
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


class Family(StrEnum):
    """Which tab a component lives under.

    Six families rather than twenty-one tabs. The grouping is navigational: every
    component keeps its own section, state and artefact inside its family.
    """

    CRAWL = "crawl"
    CONTENT = "content"
    AGENTS = "agents"
    CAPABILITIES = "capabilities"
    DELIVERY = "delivery"
    PAGE = "page"


FAMILY_LABELS: dict[Family, str] = {
    Family.CRAWL: "Crawl rules",
    Family.CONTENT: "Content",
    Family.AGENTS: "Agent instructions",
    Family.CAPABILITIES: "Capabilities",
    Family.DELIVERY: "Delivery",
    Family.PAGE: "Page quality",
}

FAMILY_BLURBS: dict[Family, str] = {
    Family.CRAWL: "Who may crawl this site, and what they may do with what they find.",
    Family.CONTENT: "What the site contains, indexed for a language model.",
    Family.AGENTS: "How an agent should behave here, and what it must not attempt.",
    Family.CAPABILITIES: "Machine-readable services an agent can connect to.",
    Family.DELIVERY: "How the site answers — headers, negotiation, in-page tools.",
    Family.PAGE: "The page itself: structure an agent can navigate.",
}


class ComponentState(StrEnum):
    LIVE = "live"
    READY = "ready"
    TEMPLATE = "template"
    MISSING = "missing"
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True, slots=True)
class Component:
    """One row of the checklist, and everything the tool knows about it."""

    key: str
    title: str
    family: Family
    priority: Priority
    layer: int
    effort: Effort
    applies: dict[SiteType, Applicability]
    verify: str
    # What we would publish, when we can. Empty where the component is a service
    # or a server behaviour rather than a file.
    artifact: str = ""
    path: str = ""
    expect: tuple[str, ...] = ()
    # True where a starting point can be offered but never published as-is.
    templated: bool = False
    # One line on why it matters, shown under the title.
    why: str = ""

    @property
    def component(self) -> str:
        """What the checklist calls the title.

        An alias rather than a rename: `readiness`, its tests and its templates
        all read `item.component`, and churning twenty call sites to save one
        property is how a refactor acquires unrelated risk.
        """
        return self.title

    def applies_to(self, site_type: SiteType) -> Applicability:
        return self.applies.get(site_type, Applicability.NO)

    @property
    def generated(self) -> bool:
        return bool(self.artifact)

    @property
    def needs_developer(self) -> bool:
        return self.effort is not Effort.DROP_IN


def _all(applicability: Applicability) -> dict[SiteType, Applicability]:
    return dict.fromkeys(SiteType, applicability)


def _mix(content: Applicability, app: Applicability, shop: Applicability):
    return {
        SiteType.CONTENT: content,
        SiteType.APP_API: app,
        SiteType.ECOMMERCE: shop,
    }


YES, COND, NO = Applicability.YES, Applicability.CONDITIONAL, Applicability.NO
JSON = ("application/json",)
TEXT = ("text/plain", "text/markdown")
XML = ("application/xml", "text/xml", "application/rss+xml")


# The checklist, verbatim in priority, layer, applicability and verify command.
COMPONENTS: tuple[Component, ...] = (
    # -- Layer 1: the page itself -------------------------------------------
    Component(
        "cls",
        "Stable layout (CLS under 0.1)",
        Family.PAGE,
        Priority.MUST,
        1,
        Effort.CODE_CHANGE,
        _all(YES),
        "npx lighthouse <site> --view (check CLS)",
        why="An agent screenshotting a page that is still moving reads the wrong thing.",
    ),
    Component(
        "semantic-html",
        "Semantic HTML (button, a, nav, main, section, article)",
        Family.PAGE,
        Priority.MUST,
        1,
        Effort.CODE_CHANGE,
        _all(YES),
        "DevTools > Elements > Accessibility tab > walk tree",
        why="Agents that walk the accessibility tree navigate by these and nothing else.",
    ),
    Component(
        "roles",
        "role + tabindex on div-pretending-to-be-button elements",
        Family.PAGE,
        Priority.MUST,
        1,
        Effort.CODE_CHANGE,
        _all(YES),
        "Accessibility tree walk; look for clickable divs without role",
        why="A clickable div with no role is invisible as a control.",
    ),
    Component(
        "cursor",
        "cursor: pointer on interactive elements",
        Family.PAGE,
        Priority.MUST,
        1,
        Effort.CODE_CHANGE,
        _all(YES),
        "Hover-test; CSS audit for missing cursor: pointer",
        why="Visual agents use the cursor to decide what is clickable.",
    ),
    Component(
        "labels",
        'label for="id" on every form input',
        Family.PAGE,
        Priority.MUST,
        1,
        Effort.CODE_CHANGE,
        _all(YES),
        "Lighthouse Accessibility audit flags missing labels",
        why="An unlabelled field cannot be filled in by anything that is not guessing.",
    ),
    Component(
        "tap-targets",
        "Tap targets at least 24x24 pixels (WCAG 2.5.8)",
        Family.PAGE,
        Priority.MUST,
        1,
        Effort.CODE_CHANGE,
        _all(YES),
        'Lighthouse: "Tap targets are sized appropriately"',
        why="Agents driving a pointer miss controls smaller than this.",
    ),
    Component(
        "overlays",
        "No ghost overlays (orphan absolute-positioned elements)",
        Family.PAGE,
        Priority.MUST,
        1,
        Effort.CODE_CHANGE,
        _all(YES),
        "Inspect for position:absolute with high z-index showing no content",
        why="An invisible overlay swallows every click an agent makes.",
    ),
    # -- Crawl rules ---------------------------------------------------------
    Component(
        "robots",
        "AI bot rules in /robots.txt",
        Family.CRAWL,
        Priority.MUST,
        2,
        Effort.DROP_IN,
        _all(YES),
        "curl <site>/robots.txt",
        artifact="robots.txt",
        path="/robots.txt",
        expect=TEXT,
        why="The one file every AI crawler reads before anything else.",
    ),
    Component(
        "content-signals",
        "Content Signals in robots.txt",
        Family.CRAWL,
        Priority.SHOULD,
        2,
        Effort.DROP_IN,
        _all(YES),
        "curl <site>/robots.txt | grep -i content-signal",
        artifact="robots.txt",
        why="States what may be done with the content, not merely who may fetch it.",
    ),
    Component(
        "web-bot-auth",
        "Web Bot Auth at CDN edge",
        Family.CRAWL,
        Priority.OPTIONAL,
        2,
        Effort.SERVER_CONFIG,
        _all(COND),
        "Check your CDN dashboard for verified-bots setting",
        why="Separates verified agents from anything claiming their user agent.",
    ),
    # -- Content -------------------------------------------------------------
    Component(
        "llms-txt",
        "/llms.txt at the domain root",
        Family.CONTENT,
        Priority.MUST,
        2,
        Effort.DROP_IN,
        _all(YES),
        "curl <site>/llms.txt",
        artifact="llms.txt",
        path="/llms.txt",
        expect=TEXT,
        why="A curated index beats leaving a model to guess which pages matter.",
    ),
    Component(
        "sitemap",
        "/sitemap.xml live and reachable",
        Family.CONTENT,
        Priority.MUST,
        2,
        Effort.DROP_IN,
        _all(YES),
        "curl <site>/sitemap.xml",
        path="/sitemap.xml",
        expect=XML,
        why="Still the cheapest complete list of what exists.",
    ),
    # -- Agent instructions --------------------------------------------------
    Component(
        "agents-md",
        "/agents.md at the domain root",
        Family.AGENTS,
        Priority.SHOULD,
        2,
        Effort.DROP_IN,
        _all(YES),
        "curl <site>/agents.md",
        artifact="agents.md",
        path="/agents.md",
        expect=TEXT,
        why="How to act here, and what not to attempt. Read, then followed.",
    ),
    # -- Capabilities --------------------------------------------------------
    Component(
        "ai-catalog",
        "Agentic Resource Discovery at /.well-known/ai-catalog.json",
        Family.CAPABILITIES,
        Priority.OPTIONAL,
        2,
        Effort.DROP_IN,
        _mix(NO, COND, COND),
        "curl <site>/.well-known/ai-catalog.json",
        artifact="ai-catalog.json",
        path="/.well-known/ai-catalog.json",
        expect=JSON,
        why="One index pointing at every service an agent can connect to.",
    ),
    Component(
        "mcp-card",
        "MCP Server Card at /.well-known/mcp/server-card.json",
        Family.CAPABILITIES,
        Priority.SHOULD,
        2,
        Effort.INFRASTRUCTURE,
        _mix(COND, YES, COND),
        "curl <site>/.well-known/mcp/server-card.json",
        path="/.well-known/mcp/server-card.json",
        expect=JSON,
        templated=True,
        why="Describes a running MCP server. Needs the server to exist first.",
    ),
    Component(
        "a2a-card",
        "A2A Agent Card at /.well-known/agent.json",
        Family.CAPABILITIES,
        Priority.OPTIONAL,
        2,
        Effort.INFRASTRUCTURE,
        _mix(NO, COND, NO),
        "curl <site>/.well-known/agent.json",
        path="/.well-known/agent.json",
        expect=JSON,
        templated=True,
        why="Declares this site as an agent other agents can call.",
    ),
    Component(
        "oauth-resource",
        "OAuth Protected Resource at /.well-known/oauth-protected-resource.json",
        Family.CAPABILITIES,
        Priority.OPTIONAL,
        2,
        Effort.INFRASTRUCTURE,
        _mix(NO, COND, COND),
        "curl <site>/.well-known/oauth-protected-resource.json",
        path="/.well-known/oauth-protected-resource.json",
        expect=JSON,
        templated=True,
        why="How an agent authenticates before touching a protected API.",
    ),
    Component(
        "skills",
        "Agent Skills at /.well-known/skills.json",
        Family.CAPABILITIES,
        Priority.OPTIONAL,
        2,
        Effort.INFRASTRUCTURE,
        _mix(NO, COND, NO),
        "curl <site>/.well-known/skills.json",
        path="/.well-known/skills.json",
        expect=JSON,
        templated=True,
        why="Named capabilities an agent can invoke directly.",
    ),
    Component(
        "api-catalog",
        "API Catalog at /.well-known/api-catalog",
        Family.CAPABILITIES,
        Priority.OPTIONAL,
        2,
        Effort.INFRASTRUCTURE,
        _mix(NO, COND, COND),
        "curl <site>/.well-known/api-catalog",
        path="/.well-known/api-catalog",
        expect=(*JSON, "text/plain"),
        templated=True,
        why="Where the site's public APIs are described.",
    ),
    Component(
        "commerce-protocols",
        "Commerce protocols (x402, MPP, UCP, ACP)",
        Family.CAPABILITIES,
        Priority.OPTIONAL,
        2,
        Effort.INFRASTRUCTURE,
        _mix(NO, NO, COND),
        "curl <site>/.well-known/ucp",
        path="/.well-known/ucp",
        expect=JSON,
        why="The machine contract for buying. Platform-provided on Shopify.",
    ),
    # -- Delivery ------------------------------------------------------------
    Component(
        "link-header",
        "Link HTTP header with agent-aware rels",
        Family.DELIVERY,
        Priority.MUST,
        2,
        Effort.SERVER_CONFIG,
        _all(YES),
        'curl -sI <site>/ | grep -i "^link:"',
        artifact="_headers",
        why="Advertises the agent surfaces without an agent having to guess paths.",
    ),
    Component(
        "markdown-negotiation",
        "Markdown negotiation (Accept: text/markdown)",
        Family.DELIVERY,
        Priority.SHOULD,
        2,
        Effort.SERVER_CONFIG,
        _mix(YES, COND, COND),
        'curl -H "Accept: text/markdown" <site>/ | wc -c',
        why="The one item with measured benefit: roughly a five-fold drop in payload.",
    ),
    Component(
        "webmcp",
        "WebMCP (navigator.modelContext)",
        Family.DELIVERY,
        Priority.SHOULD,
        2,
        Effort.CODE_CHANGE,
        _mix(COND, YES, YES),
        "Load homepage; check navigator.modelContext in console",
        templated=True,
        why="Exposes in-page tools to an agent driving the browser.",
    ),
)


BY_KEY: dict[str, Component] = {c.key: c for c in COMPONENTS}


def by_key(key: str) -> Component | None:
    return BY_KEY.get(key)


def by_family(family: Family, site_type: SiteType | None = None) -> list[Component]:
    """Components in one family, optionally filtered to a site type.

    Filtering drops only the ones marked NO. Conditionals stay: "if you run one"
    is information the operator needs, and hiding it makes the tab look shorter
    than the work actually is.
    """
    items = [c for c in COMPONENTS if c.family is family]
    if site_type is None:
        return items
    return [c for c in items if c.applies_to(site_type) is not Applicability.NO]


def applicable(site_type: SiteType) -> list[Component]:
    return [c for c in COMPONENTS if c.applies_to(site_type) is not Applicability.NO]


def for_client(site_type: SiteType) -> list[Component]:
    """Everything a person can do without a developer."""
    return [c for c in applicable(site_type) if not c.needs_developer]


def for_developer(site_type: SiteType) -> list[Component]:
    """Everything that blocks on someone with access to the stack.

    Together with `for_client` this is exactly `applicable`, with no overlap --
    asserted in the tests, because a component appearing on neither list is one
    nobody will ever do.
    """
    return [c for c in applicable(site_type) if c.needs_developer]
