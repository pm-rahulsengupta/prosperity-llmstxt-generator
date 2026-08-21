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

from app.core.components import (
    COMPONENTS,
    Applicability,
    Component,
    Priority,
    SiteType,
)

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


class CheckState(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    NOT_APPLICABLE = "not_applicable"
    # Layer 1, and anything needing a rendered page. Never counted as a pass.
    MANUAL = "manual"
    UNREACHABLE = "unreachable"


# The checklist is a projection of the component registry, not a second copy of
# it. Priorities, applicability and verify commands all live in one place now;
# this module contributes the probing and nothing else. `ChecklistItem` is an
# alias so the twenty call sites reading `item.component` keep working.
ChecklistItem = Component
CHECKLIST: tuple[Component, ...] = COMPONENTS


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
# One page per sitemap group, bounded. Enough to cover the templates a site
# actually uses without turning an audit into a crawl.
MAX_SAMPLES = 4

# Three Layer 1 items are visible in the HTML without rendering it. A static
# parse is weaker than Lighthouse -- it cannot see what CSS or JavaScript does at
# runtime -- but "no <main> element anywhere in the document" is a fact, and
# reporting it as needing a browser was giving up on a check we can make. The
# four that genuinely need rendering (layout shift, tap-target size, cursor
# styles, ghost overlays) stay manual, because guessing at them from source is
# how a passing score gets handed to a site that fails.
# Landmarks, by tag *or* by ARIA role. Diagnosed against the rendered DOM of
# prosperitymedia.com.au: the site has no `<main>` and no `<nav>` element at all,
# and carries `role="main"` and `role="banner"` instead. A tag-only check calls
# that a failure, when an agent walking the accessibility tree sees exactly the
# landmark it needs -- the role is the thing the tree is built from, and the tag
# is only the most common way to produce one.
SEMANTIC_TAGS = ("<main", "<nav", "<article", "<section", "<header", "<footer", "<aside")
LANDMARK_ROLE = re.compile(
    r"role\s*=\s*[\"']?(main|banner|navigation|contentinfo|complementary|region|article)",
    re.I,
)

# Elements that are focusable or clickable without being natively interactive.
# The first version searched for inline `onclick` alone and found none anywhere on
# the site, so the check passed a page carrying two real violations: the mobile
# menu is `<div class="hamburger" tabindex="0">` with no role, which an
# accessibility tree cannot identify as a control. Anchors and buttons are
# excluded because they are interactive by nature -- 180 of the 182 focusable
# elements on that page are `<a tabindex="-1">` in a lazy-loaded gallery, and
# flagging those would bury the two that matter.
FOCUSABLE_DIV = re.compile(r"<(?:div|span|li)[^>]*\b(?:tabindex|onclick)\s*=[^>]*>", re.I)
DIV_WITH_ROLE = re.compile(r"\brole\s*=", re.I)
INPUT_TAG = re.compile(r"<input[^>]*>", re.I)
INPUT_TYPE_HIDDEN = re.compile(r"type\s*=\s*[\"']?(hidden|submit|button)", re.I)
INPUT_ID = re.compile(r"\bid\s*=\s*[\"']?([^\"' >]+)", re.I)
LABEL_FOR = re.compile(r"<label[^>]*\bfor\s*=\s*[\"']?([^\"' >]+)", re.I)
ARIA_LABELLED = re.compile(r"\baria-label(?:ledby)?\s*=", re.I)


def _semantic_html(html: str) -> tuple[CheckState, str]:
    tags = [tag.lstrip("<") for tag in SEMANTIC_TAGS if tag in html.lower()]
    roles = sorted({m.lower() for m in LANDMARK_ROLE.findall(html)})
    found = tags + [f"role={r}" for r in roles if r not in tags]

    if len(found) >= 3:
        return CheckState.PASS, f"landmarks: {', '.join(found[:6])}"
    return CheckState.FAIL, (
        f"only {', '.join(found) or 'none'} found; agents walking the accessibility "
        "tree have little structure to navigate"
    )


def _clickable_divs(html: str) -> tuple[CheckState, str]:
    focusable = FOCUSABLE_DIV.findall(html)
    unroled = [d for d in focusable if not DIV_WITH_ROLE.search(d)]

    if not focusable:
        # Honest about the blind spot. A div made clickable purely by
        # `addEventListener`, with no tabindex and no role, is invisible to any
        # static parse -- and is also the worst version of this fault, since it is
        # not keyboard-reachable either. Passing here means "nothing visible in
        # the markup", not "audited".
        return CheckState.PASS, "no focusable or clickable div in the markup"
    if not unroled:
        return CheckState.PASS, f"{len(focusable)} focusable element(s), all with a role"
    return CheckState.FAIL, (
        f"{len(unroled)} of {len(focusable)} focusable element(s) have no role, "
        f"e.g. {unroled[0][:60]}; an agent walking the tree cannot tell what they do"
    )


def _form_labels(html: str) -> tuple[CheckState, str]:
    inputs = [i for i in INPUT_TAG.findall(html) if not INPUT_TYPE_HIDDEN.search(i)]
    if not inputs:
        return CheckState.NOT_APPLICABLE, "no form inputs on this page"

    labelled = set(LABEL_FOR.findall(html))
    unlabelled = []
    for tag in inputs:
        if ARIA_LABELLED.search(tag):
            continue
        found_id = INPUT_ID.search(tag)
        if not found_id or found_id.group(1) not in labelled:
            unlabelled.append(tag[:60])
    if not unlabelled:
        return CheckState.PASS, f"{len(inputs)} input(s), all labelled"
    return CheckState.FAIL, f"{len(unlabelled)} of {len(inputs)} input(s) have no label"


# Only the homepage is parsed, so a pass is evidence about one page rather than
# the site. Said plainly in the detail rather than implied by the score.
STATIC_LAYER1 = {
    "semantic-html": _semantic_html,
    "roles": _clickable_divs,
    "labels": _form_labels,
}

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
    site_url: str,
    user_agent: str,
    site_type: SiteType = SiteType.CONTENT,
    timeout: float = 20.0,
    sample_urls: list[str] | None = None,
) -> ReadinessReport:
    """Run every checkable item, and report the rest as needing a person.

    `sample_urls` should be one page per sitemap group. The page-level checks run
    across all of them rather than on the homepage alone, because a homepage is
    the least representative page most sites have: it is usually bespoke while the
    templates carry the structure an agent will actually meet. A service page and
    a blog post on the same site are built by different templates and fail
    differently, and auditing one of them says nothing about the other.
    """
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

        # One page per template, capped and polite. Failures here are skipped
        # rather than fatal: a sample we could not fetch should narrow the audit,
        # not fail it.
        pages: list[tuple[str, str]] = []
        if home is not None:
            pages.append((origin + "/", home.text))
        for url in (sample_urls or [])[:MAX_SAMPLES]:
            if url.rstrip("/") == origin.rstrip("/"):
                continue
            async with gate:
                sampled = await _fetch(client, url)
            if sampled is not None and sampled.status_code < 400:
                pages.append((url, sampled.text))

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

        if item.key in STATIC_LAYER1 and pages:
            # Worst answer wins. One template failing is the site failing for
            # every page built from it, and averaging would let a clean homepage
            # hide a broken service template behind it.
            results = [(url, *STATIC_LAYER1[item.key](html)) for url, html in pages]
            failed = [(url, detail) for url, state, detail in results if state is CheckState.FAIL]
            scope = f"{len(pages)} page(s) sampled"

            if failed:
                url, detail = failed[0]
                report.results.append(
                    CheckResult(
                        item,
                        CheckState.FAIL,
                        f"{detail} — on {len(failed)} of {scope}",
                        url,
                    )
                )
            elif all(state is CheckState.NOT_APPLICABLE for _, state, _ in results):
                report.results.append(
                    CheckResult(item, CheckState.NOT_APPLICABLE, results[0][2], origin + "/")
                )
            else:
                # The detail comes from a page that actually passed. Taking
                # `results[0]` reported the homepage's "no form inputs" as the
                # reason a check passed, when the pass came from a different page
                # that had labelled inputs.
                passed = next(
                    (d for _, state, d in results if state is CheckState.PASS), results[0][2]
                )
                report.results.append(
                    CheckResult(item, CheckState.PASS, f"{passed} — {scope}", origin + "/")
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
