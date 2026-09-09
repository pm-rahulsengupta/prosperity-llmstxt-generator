"""Four ways this tool read data that was there and got the wrong answer.

Grouped in one file because they are one failure shape rather than one subsystem:
a value arrives, something about its packaging or spelling is not what the reader
assumed, and the reader reports a confident wrong answer instead of no answer. Each
was found on the stranded redspot.com.au run, and each is measured in
`docs/BUILD-LOG.md` against the day it was measured.
"""

from __future__ import annotations

import asyncio
import json

from app.config import get_settings
from app.core import text
from app.core.onboarding import SiteBrief
from app.db import repo
from app.llm.client import LLMClient, LLMUsage, Stage, _unfence
from app.llm.prompts.plan import CrawlPlan, TemplateRule
from app.llm.stages import select_urls
from app.scrape.recon import PathTemplate, RobotsInfo, SiteRecon, cluster_urls
from app.scrape.sizing import CountFailure, assess, parse_results_count

# -- 1. `site:` no longer returns an index estimate ---------------------------


def _serp(count: int, organic: int) -> dict:
    return {
        "tasks": [
            {
                "result": [
                    {
                        "keyword": "site:example.com",
                        "se_results_count": count,
                        "items": [{"type": "organic"} for _ in range(organic)] + [{"type": "paid"}],
                    }
                ]
            }
        ]
    }


def test_a_count_no_larger_than_page_one_is_not_an_estimate():
    """The live shape, measured 2026-09-09 on redspot.com.au.

    Ten results shown, ten claimed. An estimate of a whole index cannot be smaller
    than the results already on the page reporting it.
    """
    result = parse_results_count(_serp(count=10, organic=10))

    assert result.failed
    assert result.reason is CountFailure.NOT_AN_ESTIMATE
    assert "page one" in result.explain()
    assert "sitemap is the only size signal" in result.explain()


def test_the_artefact_is_the_same_for_sites_of_wildly_different_size():
    """amazon.com and nytimes.com both returned 25; github.com returned 10.

    Those three do not share an index size, which is the whole argument for
    refusing the figure rather than tuning a threshold against it.
    """
    for count in (10, 25, 26):
        assert parse_results_count(_serp(count, organic=10)).failed


def test_a_real_estimate_still_reads_as_one():
    result = parse_results_count(_serp(count=12_400, organic=10))
    assert result.count == 12_400
    assert result.reason is CountFailure.OK


def test_refusing_the_figure_removes_the_warning_it_used_to_fabricate():
    """The concrete harm: a false instruction at the review gate.

    442 sitemap URLs against a reported 10 tripped the "most of what is published
    is not being kept ... cap the crawl and lean on the exclude rules" warning, on
    a site whose sitemap is fine. An operator acting on it excludes real pages.
    """
    sitemap = [f"https://redspot.com.au/p{i}" for i in range(442)]

    fabricated = assess(
        "https://redspot.com.au", sitemap, indexed=parse_results_count(_serp(10, 10))
    )
    honest = " ".join(fabricated.warnings)

    assert "is not being kept" not in honest
    assert "lean on the exclude rules" not in honest
    assert "page one" in honest
    # And the sitemap still drives the tier, exactly as before.
    assert fabricated.tier == "medium"
    assert fabricated.indexed_estimate is None


# -- 2. a gateway that fences its JSON -----------------------------------------

FENCE = "`" * 3


def test_a_fenced_response_is_read_rather_than_thrown_away():
    """Measured against OmniRoute: four models, schema-correct JSON, all fenced.

    `strict` json_schema is an OpenAI feature and a compatible gateway may accept
    the parameter and ignore it. Every stage recorded "invalid JSON" and fell back
    while the answer sat inside the string.
    """
    payload = {"site_name": "Redspot Car Rentals", "pattern": "catalog"}
    fenced = f"{FENCE}json\n{json.dumps(payload)}\n{FENCE}"

    assert json.loads(_unfence(fenced)) == payload


def test_an_unfenced_response_is_returned_byte_for_byte():
    raw = '{"site_name": "Redspot"}'
    assert _unfence(raw) is raw or _unfence(raw) == raw


def test_a_fence_inside_a_value_is_left_alone():
    """A QA finding quoting a code block must not be truncated into invalid JSON."""
    body = json.dumps({"finding": f"use {FENCE}json{FENCE} in the docs"})
    assert _unfence(body) == body
    assert json.loads(_unfence(body))["finding"].count(FENCE) == 2


def test_a_bare_fence_pair_is_not_mistaken_for_a_wrapper():
    assert _unfence(FENCE * 2) == FENCE * 2
    assert _unfence(f"{FENCE}json {{}} {FENCE}") == f"{FENCE}json {{}} {FENCE}"


def test_carriage_returns_and_a_blank_info_string_are_handled():
    payload = {"a": 1}
    assert json.loads(_unfence(f"{FENCE}\r\n{json.dumps(payload)}\r\n{FENCE}")) == payload


# -- 3. must_appear was absolute everywhere except the crawl -------------------


def _recon(paths: list[str]) -> SiteRecon:
    site = "https://www.redspot.com.au"
    urls = [f"{site}{path}" for path in paths]
    return SiteRecon(
        site_url=site,
        robots=RobotsInfo(fetched=True),
        urls=urls,
        templates=cluster_urls(urls),
    )


def _plan() -> CrawlPlan:
    return CrawlPlan(rules=[TemplateRule(template="/{slug}", action="include", priority=1)])


def test_a_named_url_survives_a_cap_that_would_have_dropped_it():
    """The redspot case: 173 named URLs, a 400 cap over 442, one page lost.

    `/vehicles/van-hire/tradies/` fell past `ordered[:page_cap]` and nothing
    reported it, because from the crawl's point of view nothing had gone wrong.
    """
    recon = _recon([f"/p{i}" for i in range(50)] + ["/tradies"])
    brief = SiteBrief(must_appear=frozenset({"https://www.redspot.com.au/tradies"}))

    without = select_urls(recon, _plan(), page_cap=10)
    with_brief = select_urls(recon, _plan(), page_cap=10, brief=brief)

    assert "https://www.redspot.com.au/tradies" not in without
    assert "https://www.redspot.com.au/tradies" in with_brief
    # A promotion, not an addition: the cap is still the cap.
    assert len(with_brief) == 10


def test_a_named_url_the_plan_excluded_is_still_crawled():
    """ "Regardless of what the numbers say" covers a rule as much as a score."""
    recon = _recon(["/keep", "/dropped"])
    plan = CrawlPlan(
        rules=[
            TemplateRule(template="/{slug}", action="include", priority=1),
            TemplateRule(template="/dropped", action="exclude", priority=1),
        ]
    )
    brief = SiteBrief(must_appear=frozenset({"https://www.redspot.com.au/dropped"}))

    assert "https://www.redspot.com.au/dropped" in select_urls(recon, plan, 10, brief=brief)


def test_a_named_url_discovery_never_found_adds_nothing():
    """Two of redspot's 173 are not in any sitemap. A priority claim is not a
    licence to fetch a URL that was never discovered."""
    recon = _recon(["/real"])
    brief = SiteBrief(must_appear=frozenset({"https://www.redspot.com.au/imaginary"}))

    selected = select_urls(recon, _plan(), page_cap=10, brief=brief)
    assert "https://www.redspot.com.au/imaginary" not in selected


def test_a_run_whose_named_set_fits_selects_exactly_what_it_did_before():
    """No brief, or a brief inside the cap, must not reorder a working run."""
    recon = _recon([f"/p{i}" for i in range(5)])
    brief = SiteBrief(must_appear=frozenset({"https://www.redspot.com.au/p0"}))

    assert sorted(select_urls(recon, _plan(), 100, brief=brief)) == sorted(
        select_urls(recon, _plan(), 100)
    )


def test_no_brief_behaves_exactly_as_it_always_has():
    recon = _recon([f"/p{i}" for i in range(50)])
    assert select_urls(recon, _plan(), 10) == select_urls(recon, _plan(), 10, brief=None)


# -- 4. one spelling of a domain, in the file as well as the tables ------------


def test_the_rendered_domain_and_the_stored_domain_agree():
    """`repo.domain_of` keys every table; `text.domain_of` names the site in the
    file. A client filed under `example.com` and greeted as `WWW.Example.com` is
    one disagreement with two visible halves."""
    for url in (
        "https://WWW.REDSPOT.COM.AU/",
        "https://www.redspot.com.au/locations/",
        "https://redspot.com.au",
        "https://Redspot.com.au/x",
    ):
        assert text.domain_of(url) == repo.domain_of(url)


def test_www_is_stripped_as_a_prefix_not_as_a_substring():
    """`replace` removes the substring wherever it occurs, which resolves a host
    to a domain that is not the client's."""
    assert text.domain_of("https://shop.www.example.com/") == "shop.www.example.com"
    assert text.domain_of("https://WWW.NRMA.COM.AU/") == "nrma.com.au"


class _Message:
    def __init__(self, content: str) -> None:
        self.content = content
        self.refusal = None


class _Choice:
    def __init__(self, content: str, finish_reason: str) -> None:
        self.message = _Message(content)
        self.finish_reason = finish_reason


class _Response:
    def __init__(self, content: str, finish_reason: str) -> None:
        self.choices = [_Choice(content, finish_reason)]
        self.usage = None
        self.model = "gateway/model"


def _decode(
    client: LLMClient,
    usage: LLMUsage,
    content: str,
    finish_reason: str = "stop",
) -> dict | None:
    """Drive `structured` over a canned reply, so the decode path is exercised
    without a network call. Patching the transport rather than reimplementing the
    branch keeps the test honest about what the caller actually runs."""

    class _Completions:
        async def create(self, **_: object) -> _Response:
            return _Response(content, finish_reason)

    class _Chat:
        completions = _Completions()

    class _Fake:
        chat = _Chat()

    client._client = _Fake()
    return asyncio.run(client.structured(Stage.PLAN, "SYSTEM", "USER", {"type": "object"}, "thing"))


# -- 5. a gateway that accepts `response_format` and drops it ------------------


def _client(base_url: str) -> tuple[LLMClient, LLMUsage]:
    settings = get_settings().model_copy(
        update={"openai_api_key": "sk-test", "openai_base_url": base_url}
    )
    usage = LLMUsage()
    return LLMClient(settings, usage), usage


def test_openai_itself_is_trusted_to_enforce_the_schema():
    client, _ = _client("")
    assert client.schema_is_enforced
    assert client._system_for("SYSTEM", {"type": "object"}, "thing") == "SYSTEM"


def test_a_compatible_gateway_is_told_the_schema_in_the_prompt():
    """OmniRoute accepted `strict` json_schema and returned four paragraphs of
    English. The parameter is not a contract off OpenAI's own endpoint."""
    client, _ = _client("https://omniroute.prosperitymedia.co/v1")
    assert not client.schema_is_enforced

    schema = {"type": "object", "required": ["groups"]}
    prompt = client._system_for("SYSTEM", schema, "intents")

    assert prompt.startswith("SYSTEM")
    assert "single JSON object and nothing else" in prompt
    assert "no markdown code fence" in prompt
    assert "intents" in prompt
    assert json.dumps(schema) in prompt


def test_an_empty_response_is_not_reported_as_malformed_json():
    """One of the redspot preflight's two calls came back empty and was recorded
    as "invalid JSON: Expecting value: line 1 column 1 (char 0)", which describes
    a parse failure that never happened."""
    client, usage = _client("https://gateway.example/v1")
    result = _decode(client, usage, content="   ", finish_reason="stop")

    assert result is None
    assert len(usage.fallbacks) == 1
    assert "empty response" in usage.fallbacks[0]
    assert "Expecting value" not in usage.fallbacks[0]


def test_a_prose_response_says_what_it_actually_said():
    """ "invalid JSON" hides the diagnosis. The opening words are the diagnosis."""
    client, usage = _client("https://gateway.example/v1")
    result = _decode(client, usage, content="**Classification: hub**\n\nThis is a small set")

    assert result is None
    assert "Classification: hub" in usage.fallbacks[0]


# -- 6. an exclude rule that excluded nothing ---------------------------------


def _shaped(paths: list[str], templates: list[str]) -> SiteRecon:
    """A recon whose templates are the ones the plan names.

    `cluster_urls` needs siblings before it will collapse a segment, so a handful
    of URLs clusters to literal paths and a plan written against `/{slug}/{slug}`
    would match nothing. A real run is not in that position: the plan is written
    from the recon's own templates.
    """
    site = "https://www.redspot.com.au"
    urls = [f"{site}{path}" for path in paths]
    return SiteRecon(
        site_url=site,
        robots=RobotsInfo(fetched=True),
        urls=urls,
        templates=[
            PathTemplate(
                template=template,
                count=len(urls),
                examples=urls,
            )
            for template in templates
        ],
    )


def test_a_specific_exclude_beats_a_broad_include():
    """The redspot case: twelve exclude rules, twelve URLs crawled anyway.

    `/customer-service/feedback/` is a member of both `/{slug}/{slug}` and
    `/{slug}/feedback`. The loop asks each template whether it is included and
    collects the URLs of the ones that are, so the exclude only ever declined to
    add its own list -- it never removed what the broader include had added.
    """
    recon = _shaped(
        ["/customer-service/feedback", "/about-us/careers", "/locations/hobart"],
        ["/{slug}/{slug}", "/{slug}/feedback"],
    )
    plan = CrawlPlan(
        rules=[
            TemplateRule(template="/{slug}/{slug}", action="include", priority=1),
            TemplateRule(template="/{slug}/feedback", action="exclude", priority=1),
        ]
    )

    selected = select_urls(recon, plan, page_cap=100)

    assert "https://www.redspot.com.au/customer-service/feedback" not in selected
    assert "https://www.redspot.com.au/about-us/careers" in selected
    assert "https://www.redspot.com.au/locations/hobart" in selected


def test_a_specific_include_survives_a_broad_exclude():
    """Precedence is specificity, not action. The narrower rule is the one that
    was written about the URL, whichever way it points."""
    recon = _shaped(
        ["/deals/pricing", "/deals/boxing-day-sale"],
        ["/{slug}/{slug}", "/{slug}/pricing"],
    )
    plan = CrawlPlan(
        rules=[
            TemplateRule(template="/{slug}/{slug}", action="exclude", priority=1),
            TemplateRule(template="/{slug}/pricing", action="include", priority=1),
        ]
    )

    selected = select_urls(recon, plan, page_cap=100)

    assert "https://www.redspot.com.au/deals/pricing" in selected
    assert "https://www.redspot.com.au/deals/boxing-day-sale" not in selected


def test_templates_at_different_depths_do_not_compete():
    """A one-segment rule says nothing about a two-segment URL."""
    recon = _shaped(["/offers", "/offers/summer"], ["/{slug}", "/{slug}/{slug}"])
    plan = CrawlPlan(
        rules=[
            TemplateRule(template="/{slug}", action="exclude", priority=1),
            TemplateRule(template="/{slug}/{slug}", action="include", priority=1),
        ]
    )

    selected = select_urls(recon, plan, page_cap=100)

    assert "https://www.redspot.com.au/offers/summer" in selected
    assert "https://www.redspot.com.au/offers" not in selected


def test_an_operator_named_url_still_outranks_an_exclude():
    """`must_appear` is the operator's own answer and beats a template rule."""
    recon = _shaped(["/customer-service/feedback"], ["/{slug}/{slug}", "/{slug}/feedback"])
    plan = CrawlPlan(
        rules=[
            TemplateRule(template="/{slug}/{slug}", action="include", priority=1),
            TemplateRule(template="/{slug}/feedback", action="exclude", priority=1),
        ]
    )
    brief = SiteBrief(
        must_appear=frozenset({"https://www.redspot.com.au/customer-service/feedback"})
    )

    assert "https://www.redspot.com.au/customer-service/feedback" in select_urls(
        recon, plan, 100, brief=brief
    )


def test_a_plan_with_no_excludes_is_untouched():
    """The common shape must not pay for the fix, in behaviour or in work."""
    recon = _recon([f"/p{i}" for i in range(20)])
    plan = CrawlPlan(rules=[TemplateRule(template="/{slug}", action="include", priority=1)])

    # 21, not 20: the homepage is always added.
    assert len(select_urls(recon, plan, page_cap=100)) == 21
