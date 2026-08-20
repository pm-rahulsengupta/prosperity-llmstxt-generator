"""Agent-readiness audit, from the checklist rather than from invention.

Every path, header and priority here comes from the *AI Agent Ready Checklist
2026* — twenty-one components across two layers, each with a stated priority and
a per-site-type applicability. Nothing in this module is a guess about where a
file should live: these are defined specs and defined locations, and the job is
to check them, not to design them.

The two layers behave completely differently and the report must not blur them.

**Layer 2 is checkable from here.** robots.txt, sitemap.xml, llms.txt, the
`/.well-known/` family, the `Link` header, Markdown negotiation, content
signals. All of it is an HTTP request and a content-type check, which is what
this module does.

**Layer 1 is not.** Cumulative layout shift, semantic HTML, `role` and
`tabindex`, tap-target sizes, ghost overlays — those need a rendered page and a
Lighthouse run. They are carried in the checklist and reported as *not checked
here*, with the verify command from the sheet, rather than being silently
dropped. A readiness score that quietly ignored a third of its own checklist
would be the most misleading number this tool could produce.

Applicability is per site type, so a score means something. An A2A agent card is
`Optional` on a content site and `Yes, if you ARE an agent` on an app — marking a
law firm down for not publishing one would make the number worse than useless.
Items that do not apply are excluded from the denominator, exactly as the AGT and
IDX rules already treat a skipped rule.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from enum import StrEnum

import httpx

__all__ = [
    "CHECKLIST",
    "Applicability",
    "CheckResult",
    "ChecklistItem",
    "Priority",
    "ReadinessReport",
    "SiteType",
    "audit_readiness",
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
    CONDITIONAL = "conditional"
    NO = "no"


class CheckState(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    NOT_APPLICABLE = "not_applicable"
    # Layer 1, and anything needing a rendered page. Never counted as a pass.
    MANUAL = "manual"
    UNREACHABLE = "unreachable"


@dataclass(frozen=True, slots=True)
class ChecklistItem:
    key: str
    component: str
    priority: Priority
    layer: int
    # Per site type. `CONDITIONAL` is the sheet's "If you run one" / "If you have
    # a public API" -- real, common, and not something a probe can settle, so it
    # is reported rather than scored.
    applies: dict[SiteType, Applicability]
    verify: str
    # The path this checks, where it is a simple fetch. Empty for the rest.
    path: str = ""
    expect: tuple[str, ...] = ()


def _all(applicability: Applicability) -> dict[SiteType, Applicability]:
    return dict.fromkeys(SiteType, applicability)


JSON = ("application/json",)
TEXT = ("text/plain", "text/markdown")
XML = ("application/xml", "text/xml", "application/rss+xml")

# The checklist, verbatim in priority, layer and applicability.
CHECKLIST: tuple[ChecklistItem, ...] = (
    # -- Layer 1: the page itself. None of these are checkable without a browser.
    ChecklistItem(
        "cls",
        "Stable layout (CLS under 0.1)",
        Priority.MUST,
        1,
        _all(Applicability.YES),
        "npx lighthouse <site> --view (check CLS)",
    ),
    ChecklistItem(
        "semantic-html",
        "Semantic HTML (button, a, nav, main, section, article)",
        Priority.MUST,
        1,
        _all(Applicability.YES),
        "DevTools > Elements > Accessibility tab > walk tree",
    ),
    ChecklistItem(
        "roles",
        "role + tabindex on div-pretending-to-be-button elements",
        Priority.MUST,
        1,
        _all(Applicability.YES),
        "Accessibility tree walk; look for clickable divs without role",
    ),
    ChecklistItem(
        "cursor",
        "cursor: pointer on interactive elements",
        Priority.MUST,
        1,
        _all(Applicability.YES),
        "Hover-test; CSS audit for missing cursor: pointer",
    ),
    ChecklistItem(
        "labels",
        'label for="id" on every form input',
        Priority.MUST,
        1,
        _all(Applicability.YES),
        "Lighthouse Accessibility audit flags missing labels",
    ),
    ChecklistItem(
        "tap-targets",
        "Tap targets at least 24x24 pixels (WCAG 2.5.8)",
        Priority.MUST,
        1,
        _all(Applicability.YES),
        'Lighthouse: "Tap targets are sized appropriately"',
    ),
    ChecklistItem(
        "overlays",
        "No ghost overlays (orphan absolute-positioned elements)",
        Priority.MUST,
        1,
        _all(Applicability.YES),
        "Inspect for position:absolute with high z-index showing no content",
    ),
    # -- Layer 2: the protocol surface. All of this is a fetch.
    ChecklistItem(
        "robots",
        "AI bot rules in /robots.txt",
        Priority.MUST,
        2,
        _all(Applicability.YES),
        "curl <site>/robots.txt",
        path="/robots.txt",
        expect=TEXT,
    ),
    ChecklistItem(
        "sitemap",
        "/sitemap.xml live and reachable",
        Priority.MUST,
        2,
        _all(Applicability.YES),
        "curl <site>/sitemap.xml",
        path="/sitemap.xml",
        expect=XML,
    ),
    ChecklistItem(
        "link-header",
        "Link HTTP header with agent-aware rels",
        Priority.MUST,
        2,
        _all(Applicability.YES),
        'curl -sI <site>/ | grep -i "^link:"',
    ),
    ChecklistItem(
        "llms-txt",
        "/llms.txt at the domain root",
        Priority.MUST,
        2,
        _all(Applicability.YES),
        "curl <site>/llms.txt",
        path="/llms.txt",
        expect=TEXT,
    ),
    ChecklistItem(
        "markdown-negotiation",
        "Markdown negotiation (Accept: text/markdown)",
        Priority.SHOULD,
        2,
        {
            SiteType.CONTENT: Applicability.YES,
            SiteType.APP_API: Applicability.CONDITIONAL,
            SiteType.ECOMMERCE: Applicability.CONDITIONAL,
        },
        'curl -H "Accept: text/markdown" <site>/ | wc -c',
    ),
    ChecklistItem(
        "mcp-card",
        "MCP Server Card at /.well-known/mcp/server-card.json",
        Priority.SHOULD,
        2,
        {
            SiteType.CONTENT: Applicability.CONDITIONAL,
            SiteType.APP_API: Applicability.YES,
            SiteType.ECOMMERCE: Applicability.CONDITIONAL,
        },
        "curl <site>/.well-known/mcp/server-card.json",
        path="/.well-known/mcp/server-card.json",
        expect=JSON,
    ),
    ChecklistItem(
        "webmcp",
        "WebMCP (navigator.modelContext)",
        Priority.SHOULD,
        2,
        {
            SiteType.CONTENT: Applicability.CONDITIONAL,
            SiteType.APP_API: Applicability.YES,
            SiteType.ECOMMERCE: Applicability.YES,
        },
        "Load homepage; check navigator.modelContext in console",
    ),
    ChecklistItem(
        "content-signals",
        "Content Signals in robots.txt",
        Priority.SHOULD,
        2,
        _all(Applicability.YES),
        "curl <site>/robots.txt | grep -i content-signal",
    ),
    ChecklistItem(
        "a2a-card",
        "A2A Agent Card at /.well-known/agent.json",
        Priority.OPTIONAL,
        2,
        {
            SiteType.CONTENT: Applicability.NO,
            SiteType.APP_API: Applicability.CONDITIONAL,
            SiteType.ECOMMERCE: Applicability.NO,
        },
        "curl <site>/.well-known/agent.json",
        path="/.well-known/agent.json",
        expect=JSON,
    ),
    ChecklistItem(
        "oauth-resource",
        "OAuth Protected Resource at /.well-known/oauth-protected-resource.json",
        Priority.OPTIONAL,
        2,
        {
            SiteType.CONTENT: Applicability.NO,
            SiteType.APP_API: Applicability.CONDITIONAL,
            SiteType.ECOMMERCE: Applicability.CONDITIONAL,
        },
        "curl <site>/.well-known/oauth-protected-resource.json",
        path="/.well-known/oauth-protected-resource.json",
        expect=JSON,
    ),
    ChecklistItem(
        "skills",
        "Agent Skills at /.well-known/skills.json",
        Priority.OPTIONAL,
        2,
        {
            SiteType.CONTENT: Applicability.NO,
            SiteType.APP_API: Applicability.CONDITIONAL,
            SiteType.ECOMMERCE: Applicability.NO,
        },
        "curl <site>/.well-known/skills.json",
        path="/.well-known/skills.json",
        expect=JSON,
    ),
    ChecklistItem(
        "api-catalog",
        "API Catalog at /.well-known/api-catalog",
        Priority.OPTIONAL,
        2,
        {
            SiteType.CONTENT: Applicability.NO,
            SiteType.APP_API: Applicability.CONDITIONAL,
            SiteType.ECOMMERCE: Applicability.CONDITIONAL,
        },
        "curl <site>/.well-known/api-catalog",
        path="/.well-known/api-catalog",
        expect=JSON + ("text/plain",),
    ),
    ChecklistItem(
        "commerce-protocols",
        "Commerce protocols (x402, MPP, UCP, ACP)",
        Priority.OPTIONAL,
        2,
        {
            SiteType.CONTENT: Applicability.NO,
            SiteType.APP_API: Applicability.NO,
            SiteType.ECOMMERCE: Applicability.CONDITIONAL,
        },
        "curl <site>/.well-known/ucp",
        path="/.well-known/ucp",
        expect=JSON,
    ),
    ChecklistItem(
        "web-bot-auth",
        "Web Bot Auth at CDN edge",
        Priority.OPTIONAL,
        2,
        _all(Applicability.CONDITIONAL),
        "Check your CDN dashboard for verified-bots setting",
    ),
)


@dataclass(slots=True)
class CheckResult:
    item: ChecklistItem
    state: CheckState
    detail: str = ""
    url: str = ""

    @property
    def scored(self) -> bool:
        """Only pass and fail move the number.

        Not-applicable, manual and unreachable are all excluded, for three
        different reasons: the first is not this site's job, the second we did not
        look at, the third we could not reach. Counting any of them as a pass is
        the inflation the rule engine already refuses elsewhere.
        """
        return self.state in (CheckState.PASS, CheckState.FAIL)


WEIGHT = {Priority.MUST: 5.0, Priority.SHOULD: 2.0, Priority.OPTIONAL: 1.0}


@dataclass(slots=True)
class ReadinessReport:
    site_url: str
    site_type: SiteType
    results: list[CheckResult] = field(default_factory=list)

    @property
    def score(self) -> int:
        earned = sum(WEIGHT[r.item.priority] for r in self.results if r.state is CheckState.PASS)
        possible = sum(WEIGHT[r.item.priority] for r in self.results if r.scored)
        return round(100 * earned / possible) if possible else 0

    @property
    def checked(self) -> list[CheckResult]:
        return [r for r in self.results if r.scored]

    @property
    def manual(self) -> list[CheckResult]:
        return [r for r in self.results if r.state is CheckState.MANUAL]

    @property
    def failures(self) -> list[CheckResult]:
        order = {Priority.MUST: 0, Priority.SHOULD: 1, Priority.OPTIONAL: 2}
        return sorted(
            (r for r in self.results if r.state is CheckState.FAIL),
            key=lambda r: order[r.item.priority],
        )

    def summary(self) -> str:
        return (
            f"{self.score}/100 across {len(self.checked)} automated checks; "
            f"{len(self.manual)} need a browser and were not checked here."
        )


# The whole checklist is nine fetches against one host. Four at a time keeps a
# small site responsive and still finishes in about a second.
MAX_CONCURRENCY = 4

CONTENT_SIGNAL = re.compile(r"content-signal\s*:", re.I)
AGENT_RELS = ("describedby", "sitemap", "api-catalog", "service-desc", "alternate")


async def _fetch(client: httpx.AsyncClient, url: str, **kwargs) -> httpx.Response | None:
    try:
        return await client.get(url, **kwargs)
    except httpx.HTTPError:
        return None


def _state_for(response: httpx.Response | None, expect: tuple[str, ...]) -> tuple[CheckState, str]:
    if response is None:
        return CheckState.UNREACHABLE, "no response"
    content_type = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
    if response.status_code >= 400:
        return CheckState.FAIL, f"{response.status_code}"
    if "html" in content_type:
        # The soft-404 again. A framework catch-all answering 200 for every path
        # would otherwise mark a site as publishing seven well-known files it
        # does not have.
        return CheckState.FAIL, f"{response.status_code} but served as HTML (soft 404)"
    if expect and content_type and not any(e in content_type for e in expect):
        return CheckState.FAIL, f"served as {content_type}"
    return CheckState.PASS, f"{response.status_code} {content_type or 'no content type'}"


async def audit_readiness(
    site_url: str, user_agent: str, site_type: SiteType = SiteType.CONTENT, timeout: float = 20.0
) -> ReadinessReport:
    """Run every checkable item, and report the rest as needing a person."""
    origin = site_url.rstrip("/")
    report = ReadinessReport(site_url=origin, site_type=site_type)

    async with httpx.AsyncClient(
        follow_redirects=True, timeout=timeout, headers={"User-Agent": user_agent}
    ) as client:
        fetchable = [i for i in CHECKLIST if i.path]
        # Capped rather than fired all at once. Nine simultaneous requests to a
        # shared-hosting WordPress site got three of them refused, and an
        # `unreachable` caused by our own impatience reads in the report as a
        # fact about the client's site. Politeness here is accuracy.
        gate = asyncio.Semaphore(MAX_CONCURRENCY)

        async def limited(path: str):
            async with gate:
                return await _fetch(client, origin + path)

        responses = await asyncio.gather(*(limited(item.path) for item in fetchable))
        by_key = dict(zip([i.key for i in fetchable], responses, strict=True))

        home = await _fetch(client, origin + "/")
        markdown = await _fetch(client, origin + "/", headers={"Accept": "text/markdown"})

    robots_body = ""
    if (robots := by_key.get("robots")) is not None and robots.status_code < 400:
        robots_body = robots.text

    for item in CHECKLIST:
        applies = item.applies.get(site_type, Applicability.NO)
        if applies is Applicability.NO:
            report.results.append(
                CheckResult(
                    item, CheckState.NOT_APPLICABLE, f"not expected on a {site_type.value} site"
                )
            )
            continue

        if item.layer == 1 or item.key in {"webmcp", "web-bot-auth"}:
            report.results.append(CheckResult(item, CheckState.MANUAL, item.verify))
            continue

        if item.key == "link-header":
            value = (home.headers.get("link") if home else "") or ""
            rels = [rel for rel in AGENT_RELS if rel in value.lower()]
            state = CheckState.PASS if rels else CheckState.FAIL
            detail = (
                f"rels: {', '.join(rels)}" if rels else "no agent-aware rels in the Link header"
            )
            report.results.append(CheckResult(item, state, detail, origin + "/"))
            continue

        if item.key == "content-signals":
            found = bool(CONTENT_SIGNAL.search(robots_body))
            report.results.append(
                CheckResult(
                    item,
                    CheckState.PASS if found else CheckState.FAIL,
                    "Content-Signal present" if found else "no Content-Signal line in robots.txt",
                    origin + "/robots.txt",
                )
            )
            continue

        if item.key == "markdown-negotiation":
            if home is None or markdown is None:
                report.results.append(CheckResult(item, CheckState.UNREACHABLE, "no response"))
                continue
            html_size, md_size = len(home.content), len(markdown.content)
            md_type = (markdown.headers.get("content-type") or "").lower()
            # Size alone is not evidence: many sites return the same HTML with a
            # different header, and a few return it byte-identical. Both have to
            # move for the negotiation to be real.
            honoured = "markdown" in md_type and md_size < html_size
            report.results.append(
                CheckResult(
                    item,
                    CheckState.PASS if honoured else CheckState.FAIL,
                    f"HTML {html_size:,} bytes vs {md_size:,} as {md_type or 'no type'}"
                    + ("" if honoured else " — the same document, so not negotiated"),
                    origin + "/",
                )
            )
            continue

        state, detail = _state_for(by_key.get(item.key), item.expect)
        if state is CheckState.FAIL and applies is Applicability.CONDITIONAL:
            # "If you run one" cannot be failed by a probe: absence is the correct
            # answer for most sites, and scoring it would punish a law firm for
            # not operating an MCP server.
            state, detail = CheckState.NOT_APPLICABLE, "only if you run one; none found"
        report.results.append(CheckResult(item, state, detail, origin + item.path))

    return report
