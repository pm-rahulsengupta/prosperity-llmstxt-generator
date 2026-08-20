"""The four LLM stages, each with the deterministic path it degrades to.

Every function here returns something usable whether or not a key is configured.
That is the property that keeps the tool honest: the heuristic path is not a
degraded mode nobody tests, it is the path the golden-file tests run on, and the
LLM is measured against it rather than assumed to beat it.
"""

from __future__ import annotations

import asyncio
import logging
import re
from urllib.parse import urlparse

from app.core.models import PageEntry, Section, ValidationIssue
from app.core.ranking import PATTERN_CATALOG, template_order
from app.llm.client import LLMClient, Stage
from app.llm.prompts import chat as chat_prompt
from app.llm.prompts import plan as plan_prompt
from app.llm.prompts import qa as qa_prompt
from app.llm.prompts import summarise as summarise_prompt
from app.llm.prompts import triage as triage_prompt
from app.llm.prompts.chat import ChatTurn
from app.llm.prompts.plan import CrawlPlan, TemplateRule
from app.llm.prompts.summarise import PageCopy, SiteBlurb
from app.llm.prompts.triage import Assignment
from app.scrape.recon import PathTemplate, SiteRecon

logger = logging.getLogger(__name__)

# Templates that are almost never worth a crawl slot on any site. These are the
# deterministic prior; the LLM may override them, a human may override the LLM.
JUNK_PATTERNS = (
    r"/page/\{",
    r"/tag/",
    r"/tags/",
    r"/author/",
    r"/category/\{[^}]+\}/page",
    r"/search",
    r"/cart",
    r"/checkout",
    r"/account",
    r"/login",
    r"/wp-json",
    r"/feed",
    r"\?",
    r"/amp$",
    r"/print$",
)
_JUNK = re.compile("|".join(JUNK_PATTERNS), re.I)

# Above this many pages sharing one template, take a sample rather than the lot.
SAMPLE_THRESHOLD = 200
SAMPLE_SIZE = 25


# -- stage 1: plan ----------------------------------------------------------


def heuristic_plan(recon: SiteRecon, page_cap: int) -> CrawlPlan:
    """The no-key plan: exclude the obvious junk, sample the repetitive, keep the rest."""
    rules: list[TemplateRule] = []
    for template in recon.templates:
        if _JUNK.search(template.template):
            rules.append(
                TemplateRule(
                    template=template.template,
                    action="exclude",
                    priority=5,
                    reason="pagination, archive or utility URL",
                )
            )
        elif template.count > SAMPLE_THRESHOLD:
            rules.append(
                TemplateRule(
                    template=template.template,
                    action="sample",
                    priority=3,
                    reason=f"{template.count} near-identical URLs; a sample represents them",
                )
            )
        else:
            rules.append(
                TemplateRule(
                    template=template.template,
                    action="include",
                    # Shallower templates are more likely to be the pages that
                    # explain the site, so they go first when the budget binds.
                    priority=min(5, max(1, template.max_depth or 1)),
                    reason="",
                )
            )

    return CrawlPlan(
        site_name="",
        site_pattern=PATTERN_CATALOG,
        rules=rules,
        requires_js=False,
        recommended_page_cap=page_cap,
        reasoning="No LLM key configured; templates classified by URL shape alone.",
        source="heuristic",
    )


async def plan_crawl(client: LLMClient, brief: str, recon: SiteRecon, page_cap: int) -> CrawlPlan:
    fallback = heuristic_plan(recon, page_cap)
    if not client.enabled:
        return fallback

    data = await client.structured(
        stage=Stage.PLAN,
        system=plan_prompt.SYSTEM,
        user=plan_prompt.build_user_message(brief, page_cap),
        schema=plan_prompt.schema(),
        schema_name="crawl_plan",
    )
    if data is None:
        return fallback

    parsed = CrawlPlan.from_dict({**data, "source": "llm"})
    if not parsed.rules:
        client.usage.record_fallback(Stage.PLAN, "plan contained no rules")
        return fallback

    # A model that skips templates must not silently drop them from the crawl.
    # Anything it did not rule on keeps its heuristic rule.
    ruled = {rule.template for rule in parsed.rules}
    parsed.rules.extend(rule for rule in fallback.rules if rule.template not in ruled)
    return parsed


def select_urls(recon: SiteRecon, plan: CrawlPlan, page_cap: int) -> list[str]:
    """Turn a plan into the actual crawl list, in priority order and inside budget."""
    by_template: dict[str, list[str]] = {
        template.template: _urls_for(template, recon) for template in recon.templates
    }

    selected: list[tuple[int, str]] = []
    sampled: list[tuple[int, list[str]]] = []
    for template_name, urls in by_template.items():
        rule = plan.rule_for(template_name)
        if rule is None or not rule.includes:
            continue
        if rule.sample_only:
            chosen = _sample(urls, SAMPLE_SIZE)
            sampled.append((rule.priority, [u for u in urls if u not in set(chosen)]))
        else:
            chosen = urls
        selected.extend((rule.priority, url) for url in chosen)

    # A flat site -- WordPress, most Shopify themes -- clusters into a single
    # `/{slug}` template holding almost every page, and a planner looking only at
    # path shape can do nothing but mark it "sample". Taking a flat 25 there would
    # leave a 400-page budget 94% unspent and the file thinner than it needed to be.
    # So a sample is a floor, not a quota: once the plan is applied, leftover budget
    # is spent topping the sampled templates back up in priority order.
    if page_cap > 0 and len(selected) < page_cap:
        for priority, remainder in sorted(sampled, key=lambda pair: pair[0]):
            headroom = page_cap - len(selected)
            if headroom <= 0:
                break
            selected.extend((priority, url) for url in _sample(remainder, headroom))

    selected.sort(key=lambda pair: pair[0])
    ordered = list(dict.fromkeys(url for _, url in selected))

    # The homepage is the single most useful page in the file and can be excluded by
    # an over-eager rule. It is always crawled.
    home = recon.site_url.rstrip("/") + "/"
    if home not in ordered and recon.site_url not in ordered:
        ordered.insert(0, home)

    return ordered[:page_cap] if page_cap > 0 else ordered


def _urls_for(template: PathTemplate, recon: SiteRecon) -> list[str]:
    """Members of a template, recovered from the recon URL list.

    Matching by shape rather than re-clustering: `cluster_urls` decides a segment is
    variable by looking at all its siblings at once, so running it per URL would
    collapse nothing and produce a different answer than the clustering the plan was
    written against.
    """
    return [url for url in recon.urls if _matches(url, template.template)]


def _matches(url: str, template: str) -> bool:
    segments = [s for s in urlparse(url).path.strip("/").split("/") if s]
    parts = [p for p in template.strip("/").split("/") if p]
    if len(segments) != len(parts):
        return False
    return all(
        part.startswith("{") or part == segment
        for part, segment in zip(parts, segments, strict=True)
    )


def _sample(urls: list[str], size: int) -> list[str]:
    """Evenly spaced, not random: reproducible, and it spans the whole set."""
    if len(urls) <= size:
        return urls
    step = len(urls) / size
    return [urls[int(i * step)] for i in range(size)]


# -- stage 2: triage --------------------------------------------------------


async def triage_pages(
    client: LLMClient,
    entries: list[PageEntry],
    pattern: str,
    scores: dict[str, float],
) -> dict[str, Assignment]:
    """Section assignments by URL. Empty dict means "keep every heuristic result"."""
    if not client.enabled or not entries:
        return {}

    sections = template_order(pattern)
    results: dict[str, Assignment] = {}

    for batch in triage_prompt.batches(entries):
        known = {entry.url for entry in batch}
        data = await client.structured(
            stage=Stage.TRIAGE,
            system=triage_prompt.SYSTEM,
            user=triage_prompt.build_user_message(batch, sections, scores),
            schema=triage_prompt.schema(sections),
            schema_name="section_assignments",
        )
        if data is None:
            # One failed batch keeps its heuristic sections; the rest carry on.
            continue
        for assignment in triage_prompt.parse(data, known):
            results[assignment.url] = assignment

    return results


# -- stage 3: summarise -----------------------------------------------------


async def summarise_site(
    client: LLMClient, site_url: str, site_name: str, entries: list[PageEntry]
) -> SiteBlurb | None:
    if not client.enabled or not entries:
        return None
    data = await client.structured(
        stage=Stage.SUMMARISE,
        system=summarise_prompt.SITE_SYSTEM,
        user=summarise_prompt.build_site_message(site_url, site_name, entries),
        schema=summarise_prompt.site_schema(),
        schema_name="site_blurb",
    )
    return summarise_prompt.parse_site(data) if data else None


async def summarise_pages(
    client: LLMClient, entries: list[PageEntry], concurrency: int = 4
) -> dict[str, PageCopy]:
    """Titles and descriptions by URL, for whatever came back."""
    if not client.enabled or not entries:
        return {}

    limiter = asyncio.Semaphore(concurrency)

    async def one(batch: list[PageEntry]) -> list[PageCopy]:
        known = {entry.url for entry in batch}
        async with limiter:
            data = await client.structured(
                stage=Stage.SUMMARISE,
                system=summarise_prompt.PAGE_SYSTEM,
                user=summarise_prompt.build_page_message(batch),
                schema=summarise_prompt.page_schema(),
                schema_name="page_copy",
            )
        return summarise_prompt.parse_pages(data, known) if data else []

    batches = [
        entries[i : i + summarise_prompt.BATCH_SIZE]
        for i in range(0, len(entries), summarise_prompt.BATCH_SIZE)
    ]
    results = await asyncio.gather(*(one(batch) for batch in batches))
    return {copy.url: copy for batch in results for copy in batch}


# -- stage 4: QA ------------------------------------------------------------


async def review_output(
    client: LLMClient, llmstxt: str, mechanical: list[ValidationIssue]
) -> list[ValidationIssue]:
    if not client.enabled or not llmstxt.strip():
        return []
    data = await client.structured(
        stage=Stage.QA,
        system=qa_prompt.SYSTEM,
        user=qa_prompt.build_user_message(llmstxt, mechanical),
        schema=qa_prompt.schema(),
        schema_name="spec_review",
    )
    return qa_prompt.parse(data).findings if data else []


# -- stage 5: chat editing --------------------------------------------------


async def apply_chat_turn(
    client: LLMClient,
    request: str,
    site_name: str,
    site_summary: str,
    sections: list[Section],
    optional: list[PageEntry],
    excluded: list[str],
) -> ChatTurn:
    """One conversational edit. Returns operations, never a rendered file.

    A refusal here is a real answer: with no key configured there is nothing
    sensible to fall back to, because unlike the other four stages there is no
    deterministic version of "do what this sentence asks".
    """
    if not client.enabled:
        return ChatTurn(rejected="Editing by chat needs an OpenAI key; none is configured.")
    if not request.strip():
        return ChatTurn(rejected="Nothing to do.")

    section_names = [section.name for section in sections]
    known_urls = {page.url for section in sections for page in section.pages}
    known_urls.update(page.url for page in optional)
    known_urls.update(excluded)

    data = await client.structured(
        stage=Stage.CHAT,
        system=chat_prompt.SYSTEM,
        user=chat_prompt.build_user_message(
            request, site_name, site_summary, sections, optional, excluded
        ),
        schema=chat_prompt.schema(section_names, sorted(known_urls)),
        schema_name="edit_operations",
    )
    if data is None:
        return ChatTurn(rejected="The model did not return a usable edit. Nothing was changed.")

    return chat_prompt.parse(data)
