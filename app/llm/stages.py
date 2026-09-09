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

from app.core.copyrules import check_all
from app.core.models import PageEntry, Section, ValidationIssue
from app.core.onboarding import SiteBrief
from app.core.ranking import PATTERN_CATALOG, template_order
from app.llm.client import LLMClient, Stage
from app.llm.prompts import chat as chat_prompt
from app.llm.prompts import intent as intent_prompt
from app.llm.prompts import onboard as onboard_prompt
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
# Every single-word path segment is bounded. Unanchored, `/search` matched
# `/services/search-engine-optimisation/{slug}`, `/cart` matched
# `/services/cartage-and-logistics`, `/account` matched `/accounting-services`
# and `/feed` matched `/feedback` -- so a client's actual service pages were
# dropped from the crawl plan before the model or the operator ever saw them,
# and the exclusion read as a considered decision rather than a bug.
#
# `(?=/|$|\?)` rather than ``: a word boundary is satisfied by the `-` in
# `/search-engine`, which is exactly the case that was wrong.
JUNK_PATTERNS = (
    r"/page/\{",
    r"/tag/",
    r"/tags/",
    r"/author/",
    r"/category/\{[^}]+\}/page",
    r"/search(?=/|$|\?)",
    r"/cart(?=/|$|\?)",
    r"/checkout(?=/|$|\?)",
    r"/account(?=/|$|\?)",
    r"/login(?=/|$|\?)",
    r"/wp-json",
    r"/feed(?=/|$|\?)",
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


async def plan_crawl(
    client: LLMClient,
    brief: str,
    recon: SiteRecon,
    page_cap: int,
    site_brief: SiteBrief | None = None,
) -> CrawlPlan:
    fallback = heuristic_plan(recon, page_cap)
    if not client.enabled:
        return fallback

    data = await client.structured(
        stage=Stage.PLAN,
        system=plan_prompt.SYSTEM,
        user=plan_prompt.build_user_message(brief, page_cap, site_brief),
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


async def classify_groups(client: LLMClient, rows: list) -> dict[str, tuple[str, str]]:
    """Label each sitemap group as editorial / faceted / hub / utility.

    Degrades to the deterministic prior rather than to nothing, in keeping with
    every other stage here: with no key, uniformity does the classifying, which
    is the same signal the prompt tells the model to weight most heavily.
    """
    if not rows:
        return {}
    if not client.enabled:
        return _heuristic_intents(rows)

    data = await client.structured(
        stage=Stage.PLAN,
        system=intent_prompt.SYSTEM,
        user=intent_prompt.build_user_message(rows),
        schema=intent_prompt.schema(),
        schema_name="group_intents",
    )
    if data is None:
        return _heuristic_intents(rows)

    parsed = intent_prompt.parse(data, {row.group_key for row in rows})
    # A group the model skipped keeps the heuristic label rather than staying
    # blank: an unlabelled group reads as "nothing known" when in fact its shape
    # is known and was simply not commented on.
    return {**_heuristic_intents(rows), **parsed}


def _heuristic_intents(rows: list) -> dict[str, tuple[str, str]]:
    """Intent from shape alone. No key required, and no guessing about value."""
    out: dict[str, tuple[str, str]] = {}
    for row in rows:
        name = row.group_key.lower()
        if any(token in name for token in ("tag", "author", "search", "page-sitemap-")):
            out[row.group_key] = ("utility", "sitemap name is an archive or utility one")
        elif row.url_count >= 200 and row.url_count / max(1, row.template_diversity) >= 100:
            # The ratio, not strict uniformity. A real facet group rarely
            # collapses to exactly one template -- CarsGuide's `AllNew_Make`
            # produces a handful as segment counts vary -- but thousands of URLs
            # across a couple of shapes is still machine-generated, and the
            # earlier `diversity == 1` test missed every such group.
            out[row.group_key] = (
                "faceted",
                f"{row.url_count:,} URLs across only {row.template_diversity} path template(s)",
            )
        elif row.url_count <= 20 and row.template_diversity <= 3:
            out[row.group_key] = ("hub", "a handful of pages, few shapes")
        elif row.template_diversity > 3:
            out[row.group_key] = ("editorial", f"{row.template_diversity} distinct URL shapes")
        else:
            out[row.group_key] = ("unknown", "shape alone is not conclusive")
    return out


async def suggest_brief(
    client: LLMClient,
    site_url: str,
    site_summary: str,
    platform: str,
    groups: list[tuple[str, int]],
    sample_urls: list[str],
    homepage_text: str = "",
    known_urls: list[str] | None = None,
) -> dict:
    """Propose brief answers from site evidence. Returns {} when it cannot.

    Empty rather than a heuristic fallback, and the distinction matters here more
    than elsewhere: everywhere else a fallback keeps the pipeline running, but a
    guessed *brief* would be adopted as the operator's own stated intent and then
    drive every downstream decision. A blank form is honest; a form pre-filled by
    keyword matching looks considered and is not.
    """
    if not client.enabled:
        return {}

    data = await client.structured(
        stage=Stage.PLAN,
        system=onboard_prompt.SYSTEM,
        user=onboard_prompt.build_user_message(
            site_url, site_summary, platform, groups, sample_urls, homepage_text
        ),
        schema=onboard_prompt.schema(),
        schema_name="brief_suggestion",
    )
    if data is None:
        return {}

    parsed = onboard_prompt.parse(data)
    # Verify the globs against real URLs before they reach the form. A pattern
    # matching nothing looks like a decision and is approved without testing.
    dropped: list[str] = []
    for key in ("valuable", "noise"):
        if not parsed.get(key):
            continue
        # Validated against every URL the site declares, not the sample shown to
        # the model. The model also reads the homepage, so it legitimately
        # proposes patterns for pages outside a 40-URL sample -- checking against
        # the sample alone dropped `/seo-agency/*` on a site that plainly has one,
        # which is the guard doing more damage than the thing it guards against.
        kept, missed = onboard_prompt.keep_matching(
            parsed[key].splitlines(), known_urls or sample_urls
        )
        parsed[key] = "\n".join(kept)
        dropped.extend(missed)
    if dropped:
        parsed["_dropped"] = dropped
    return parsed


def select_urls(
    recon: SiteRecon,
    plan: CrawlPlan,
    page_cap: int,
    brief: SiteBrief | None = None,
) -> list[str]:
    """Turn a plan into the actual crawl list, in priority order and inside budget.

    `brief` carries the operator's answers. Only ``must_appear`` is read here, and
    it is read because the onboarding form promises it is: the question is labelled
    *"Absolute. Joins the identity set, which no traffic rule can exclude"*, and
    until now that was true of exactly one thing -- the group verdict in
    `core/metrics._apply_overrides`. It was never true of the crawl. A URL the
    operator named could be excluded by a template rule, or fall past
    ``ordered[:page_cap]``, and a page that is not fetched cannot appear in any
    file assembled afterwards.

    Measured on the stranded redspot.com.au run: 173 named URLs, a plan with 44
    include rules and a 400-page cap over 442 sitemap URLs, and
    ``/vehicles/van-hire/tradies/`` fell off the end of the truncation. Nothing
    reported it, because from the crawl's point of view nothing had gone wrong.
    """
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
    ordered = [url for url in ordered if url not in _excluded(ordered, plan)]

    # The homepage is the single most useful page in the file and can be excluded by
    # an over-eager rule. It is always crawled.
    home = recon.site_url.rstrip("/") + "/"
    if home not in ordered and recon.site_url not in ordered:
        ordered.insert(0, home)

    return _with_required(ordered, recon, brief, page_cap)


def _specificity(template: str) -> tuple[int, int]:
    """How precisely a template names a URL. Higher is more specific.

    Literal segments first, then length. `/{slug}/feedback` names one page;
    `/{slug}/{slug}` names every two-segment path on the site. Both match
    `/customer-service/feedback/`, and only one of them was written about it.
    """
    parts = [p for p in template.strip("/").split("/") if p]
    return (sum(1 for p in parts if "{" not in p), len(template))


def _excluded(urls: list[str], plan: CrawlPlan) -> set[str]:
    """URLs an exclude rule names more precisely than any include rule does.

    The loop above asks each *template* whether it is included and collects the
    URLs of the ones that are. A URL matches by shape, so it belongs to every
    template it fits -- `/customer-service/feedback/` is a member of both
    `/{slug}/{slug}` and `/{slug}/feedback` -- and an exclude rule therefore only
    ever declined to add its own list. It never removed what a broader include had
    already added. Excluding was a no-op wherever a wider include existed, which on
    any real plan is everywhere.

    Measured on redspot.com.au: the planner wrote twelve exclude rules -- careers
    sub-pages, a damage report form, a feedback form, a sponsorship page -- every
    one of them naming a real cluster, and all twelve URLs were selected for the
    crawl. Nothing reported it. This is the control the review gate is built
    around: "one line here can exclude four thousand URLs before anything is
    fetched", and on a site the size of CarsGuide it is also the bill.

    Precedence is by specificity rather than by rule order, because the planner
    emits general and specific rules together and neither position nor priority
    says which was meant to win. The rule that names a URL most precisely is the
    one written about it. A tie -- two templates equally specific, one including
    and one excluding -- resolves to exclude, which is the recoverable direction:
    a page wrongly left out is visible as a gap at the review gate, and a page
    wrongly fetched has already been paid for and may already be in the file.

    `must_appear` still overrides this. An operator naming a URL outranks a
    template rule, and `_with_required` runs after.
    """
    rules = [(rule, _specificity(rule.template)) for rule in plan.rules]
    if not any(not rule.includes for rule, _ in rules):
        return set()

    # Grouped by segment count so each URL is tested only against the templates it
    # could possibly match. Without it this is every URL against every rule, which
    # on CarsGuide's 11,909 URLs and 397 templates is 4.7M shape comparisons.
    by_depth: dict[int, list] = {}
    for rule, weight in rules:
        depth = len([p for p in rule.template.strip("/").split("/") if p])
        by_depth.setdefault(depth, []).append((rule, weight))

    excluded: set[str] = set()
    for url in urls:
        segments = [s for s in urlparse(url).path.strip("/").split("/") if s]
        matches = [
            (weight, rule)
            for rule, weight in by_depth.get(len(segments), ())
            if _matches(url, rule.template)
        ]
        if not matches:
            continue
        best = max(weight for weight, _ in matches)
        # Exclude wins a tie: see the docstring on which direction is recoverable.
        if any(not rule.includes for weight, rule in matches if weight == best):
            excluded.add(url)
    return excluded


def _with_required(
    ordered: list[str],
    recon: SiteRecon,
    brief: SiteBrief | None,
    page_cap: int,
) -> list[str]:
    """Apply the cap without dropping a URL the operator declared must appear.

    The named URLs move to the front rather than being appended after the
    truncation. Appending would keep them in the list and change which page is
    dropped for each one added, silently trading an operator's explicit choice
    against the planner's priority order at the boundary. Promoting states the
    precedence the form already claims: what a person named outranks what a
    template inferred.

    Matched against the recon inventory, so a typo, a stale URL or a page the
    sitemap does not list adds nothing to the crawl -- `must_appear` is a claim
    about priority, not a licence to fetch a URL discovery never found. Two of
    redspot's 173 are exactly that case.

    Order among the promoted URLs is the plan's own, so a run whose named set is
    already inside the cap selects precisely what it selected before. Nothing here
    can lengthen a crawl beyond `page_cap`.
    """
    if not brief or not brief.must_appear:
        return ordered[:page_cap] if page_cap > 0 else ordered

    known = set(recon.urls)
    required = {url for url in brief.must_appear if url in known}
    if not required:
        return ordered[:page_cap] if page_cap > 0 else ordered

    promoted = [url for url in ordered if url in required]
    # A named URL that discovery found but the plan excluded still belongs in the
    # crawl: "regardless of what the numbers say" covers a rule as much as a score.
    promoted += [url for url in recon.urls if url in required and url not in set(ordered)]
    rest = [url for url in ordered if url not in required]

    return (promoted + rest)[:page_cap] if page_cap > 0 else promoted + rest


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


async def enforce_copy_rules(
    client: LLMClient, entries: list[PageEntry], max_retry_batch: int = 60
) -> tuple[int, list[str]]:
    """Check every link line, regenerate what fails once, flag what still fails.

    "Regenerate once, then flag" rather than loop-until-clean: a model that could not
    satisfy the constraint on the second attempt will usually not satisfy it on the
    fifth either, and an unbounded retry loop on 500 pages is real money. What is left
    is reported rather than quietly shipped.

    Returns (how many were fixed, the descriptions of what still fails).
    """
    verdicts = {v.url: v for v in check_all(entries)}
    failing = [e for e in entries if not verdicts[e.url].ok]
    if not failing:
        return 0, []

    logger.info("copy check: %d of %d link lines failed", len(failing), len(entries))

    if client.enabled and failing:
        by_url = {e.url: e for e in entries}
        for start in range(0, min(len(failing), max_retry_batch), summarise_prompt.BATCH_SIZE):
            batch = failing[start : start + summarise_prompt.BATCH_SIZE]
            problems = "\n".join(f"- {verdicts[e.url].describe()}" for e in batch)
            data = await client.structured(
                stage=Stage.SUMMARISE,
                system=summarise_prompt.PAGE_SYSTEM
                + "\n\nThese specific lines were rejected. Rewrite them so each problem "
                "is resolved, keeping the same URLs:\n" + problems,
                user=summarise_prompt.build_page_message(batch),
                schema=summarise_prompt.page_schema(),
                schema_name="page_copy",
            )
            if data is None:
                continue
            for copy in summarise_prompt.parse_pages(data, {e.url for e in batch}):
                if (entry := by_url.get(copy.url)) is not None:
                    entry.title = copy.title or entry.title
                    entry.description = copy.description or entry.description

    after = {v.url: v for v in check_all(entries)}
    still_failing = [after[e.url].describe() for e in entries if not after[e.url].ok]
    fixed = len(failing) - len(still_failing)
    if still_failing:
        logger.warning("copy check: %d line(s) still failing after one rewrite", len(still_failing))
    return fixed, still_failing
