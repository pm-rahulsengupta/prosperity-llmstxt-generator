"""Probing a site's agent-facing surfaces.

Everything the generator may claim comes from here, so most of these test what the
probe *refuses* to confirm. The distinctions are drawn from live measurements taken
2026-08-20:

    allbirds.com          agents.md present, real UCP, MCP on myshopify.com
    vercel.com            agents.md answers 200 text/html -- a soft-404
    prosperitymedia.com.au, carsguide.com.au, stripe.com   honest 404s

The vercel case is the one that matters. A naive `status == 200` check would have
the tool "audit" a React bundle and then generate a file claiming the site already
publishes one.
"""

from __future__ import annotations

import httpx
import pytest

from app.scrape.agents_probe import (
    ProbeResult,
    Surface,
    SurfaceState,
    UcpProfile,
    UcpService,
    _classify,
    detect_platform,
    parse_ucp,
)

TEXT_TYPES = ("text/markdown", "text/plain", "text/x-markdown", "application/markdown")
JSON_TYPES = ("application/json", "application/ld+json")


def response(status: int, content_type: str = "") -> httpx.Response:
    headers = {"content-type": content_type} if content_type else {}
    return httpx.Response(status_code=status, headers=headers, text="body")


# -- classification ----------------------------------------------------------


def test_a_markdown_file_is_present():
    state, _ = _classify(response(200, "text/markdown; charset=utf-8"), TEXT_TYPES)
    assert state is SurfaceState.PRESENT


def test_plain_text_counts_as_present():
    """Plenty of sites serve these as text/plain; that is not a defect."""
    state, _ = _classify(response(200, "text/plain"), TEXT_TYPES)
    assert state is SurfaceState.PRESENT


def test_a_200_of_html_is_a_soft_404_not_a_file():
    """The vercel.com case, and the whole reason status alone is not enough.

    A framework catch-all answers 200 for every path, so the status carries no
    information and the content type carries all of it.
    """
    state, content_type = _classify(response(200, "text/html; charset=utf-8"), TEXT_TYPES)

    assert state is SurfaceState.SOFT_404
    assert state is not SurfaceState.PRESENT
    assert content_type == "text/html"


def test_a_real_404_is_absent():
    state, _ = _classify(response(404, "text/html"), TEXT_TYPES)
    assert state is SurfaceState.ABSENT


@pytest.mark.parametrize("status", [401, 403, 410, 500, 503])
def test_any_error_status_is_absent_not_present(status):
    state, _ = _classify(response(status, "text/markdown"), TEXT_TYPES)
    assert state is SurfaceState.ABSENT


def test_an_unexpected_content_type_is_flagged_rather_than_accepted():
    """A file served as application/octet-stream is published wrongly.

    Distinguished from absence so the report can say "fix the content type"
    instead of "publish a file", which are different jobs.
    """
    state, _ = _classify(response(200, "application/octet-stream"), TEXT_TYPES)
    assert state is SurfaceState.WRONG_TYPE


def test_a_missing_content_type_is_accepted():
    """Some servers send none. Absence of a header is not evidence of HTML."""
    state, _ = _classify(response(200, ""), TEXT_TYPES)
    assert state is SurfaceState.PRESENT


def test_json_is_expected_for_ucp_and_markdown_is_not():
    assert _classify(response(200, "application/json"), JSON_TYPES)[0] is SurfaceState.PRESENT
    assert _classify(response(200, "text/markdown"), JSON_TYPES)[0] is SurfaceState.WRONG_TYPE


# -- the states carry different meanings -------------------------------------


def test_unreachable_is_not_evidence_about_the_site():
    """A network failure is a fact about us.

    Reporting it as "they do not publish one" would put our own timeout into a
    client-facing audit as a finding against them.
    """
    assert not SurfaceState.UNREACHABLE.is_evidence
    for state in (SurfaceState.PRESENT, SurfaceState.ABSENT, SurfaceState.SOFT_404):
        assert state.is_evidence


def test_only_present_is_usable():
    assert SurfaceState.PRESENT.usable
    for state in SurfaceState:
        if state is not SurfaceState.PRESENT:
            assert not state.usable, state


def test_each_state_explains_its_cause():
    """The operator needs to know why, not only that."""
    soft = Surface(url="https://x.com/agents.md", state=SurfaceState.SOFT_404, status=200)
    assert "HTML" in soft.describe()
    assert "not published" in soft.describe()

    wrong = Surface(
        url="https://x.com/agents.md",
        state=SurfaceState.WRONG_TYPE,
        content_type="application/octet-stream",
    )
    assert "text/markdown" in wrong.describe()

    dead = Surface(
        url="https://x.com/agents.md", state=SurfaceState.UNREACHABLE, detail="ReadTimeout"
    )
    assert "could not be checked" in dead.describe()


# -- UCP parsing -------------------------------------------------------------

# Trimmed from the live allbirds.com/.well-known/ucp document.
LIVE_UCP = """
{"ucp":{"version":"2026-04-08",
"supported_versions":{"2026-04-08":"https://weareallbirds.myshopify.com/.well-known/ucp/2026-04-08",
"2026-01-23":"https://weareallbirds.myshopify.com/.well-known/ucp/2026-01-23"},
"services":{"dev.ucp.shopping":[
{"version":"2026-04-08","spec":"https://ucp.dev/2026-04-08/specification/overview/",
"transport":"mcp","endpoint":"https://weareallbirds.myshopify.com/api/ucp/mcp",
"schema":"https://ucp.dev/2026-04-08/services/shopping/mcp.openrpc.json"}]}}}
"""


def test_a_live_ucp_profile_parses():
    profile = parse_ucp(LIVE_UCP)

    assert profile is not None
    assert profile.version == "2026-04-08"
    assert profile.supported_versions == ("2026-01-23", "2026-04-08")
    assert profile.mcp_endpoints == ("https://weareallbirds.myshopify.com/api/ucp/mcp",)


def test_a_profile_with_no_mcp_service_yields_no_mcp_endpoint():
    """The correct outcome is silence, not a guessed default."""
    profile = parse_ucp(
        '{"ucp":{"version":"2026-04-08","services":{"dev.ucp.shopping":'
        '[{"transport":"rest","endpoint":"https://x.com/api/ucp"}]}}}'
    )

    assert profile is not None
    assert profile.mcp_endpoints == ()


@pytest.mark.parametrize(
    "body",
    ["", "not json", "[]", "null", '"a string"', "{}", '{"ucp":{}}'],
)
def test_a_malformed_profile_is_none_rather_than_half_built(body):
    """A partially parsed profile is how an invented endpoint gets through."""
    assert parse_ucp(body) is None


def test_a_service_without_an_endpoint_is_skipped():
    profile = parse_ucp(
        '{"ucp":{"version":"1","services":{"dev.ucp.shopping":[{"transport":"mcp"}]}}}'
    )
    assert profile is not None
    assert profile.mcp_endpoints == ()


def test_a_flat_document_without_the_ucp_wrapper_still_parses():
    """Tolerant about the wrapper, strict about the contents."""
    profile = parse_ucp(
        '{"version":"2026-04-08","services":{"dev.ucp.shopping":'
        '[{"transport":"mcp","endpoint":"https://x.com/api/ucp/mcp"}]}}'
    )
    assert profile is not None
    assert profile.mcp_endpoints == ("https://x.com/api/ucp/mcp",)


# -- platform detection ------------------------------------------------------


def test_shopify_is_detected_from_its_own_ucp_endpoint():
    """Stronger than HTML sniffing: Shopify writes this profile itself."""
    result = ProbeResult(site_url="https://x.com", ucp_profile=parse_ucp(LIVE_UCP))
    assert detect_platform(result) == "shopify"


def test_shopify_is_detected_from_headers_when_there_is_no_ucp():
    result = ProbeResult(site_url="https://x.com")
    assert detect_platform(result, {"X-ShopId": "12345", "Server": "Shopify"}) == "shopify"


def test_an_unknown_platform_stays_unknown():
    """Guessing here changes which artefact the operator is handed."""
    result = ProbeResult(site_url="https://x.com")
    assert detect_platform(result, {"server": "nginx"}) == "unknown"


# -- what may be claimed -----------------------------------------------------


def test_nothing_may_be_claimed_without_a_ucp_profile():
    """The load-bearing invariant of the whole feature."""
    result = ProbeResult(site_url="https://prosperitymedia.com.au")

    assert result.verified_endpoints == ()
    assert not result.has_ucp
    assert not result.has_agents_md


def test_a_soft_404_does_not_count_as_having_a_file():
    """vercel.com, in one assertion."""
    result = ProbeResult(
        site_url="https://vercel.com",
        agents_md=Surface(
            url="https://vercel.com/agents.md",
            state=SurfaceState.SOFT_404,
            status=200,
            content_type="text/html",
        ),
    )

    assert not result.has_agents_md


def test_verified_endpoints_come_only_from_the_parsed_profile():
    result = ProbeResult(
        site_url="https://x.com",
        ucp_profile=UcpProfile(
            version="2026-04-08",
            services=(
                UcpService(
                    name="dev.ucp.shopping",
                    version="1",
                    transport="mcp",
                    endpoint="https://x.com/api/ucp/mcp",
                ),
                UcpService(
                    name="dev.ucp.shopping",
                    version="1",
                    transport="rest",
                    endpoint="https://x.com/api/ucp/rest",
                ),
            ),
        ),
    )

    # Only the MCP binding, and only because the document said so.
    assert result.verified_endpoints == ("https://x.com/api/ucp/mcp",)


def test_the_summary_lists_every_probed_surface():
    result = ProbeResult(
        site_url="https://x.com",
        agents_md=Surface(url="https://x.com/agents.md", state=SurfaceState.ABSENT, status=404),
        llms_txt=Surface(
            url="https://x.com/llms.txt", state=SurfaceState.PRESENT, content_type="text/plain"
        ),
    )
    summary = result.summary()

    assert "agents.md" in summary and "llms.txt" in summary


# -- politeness is accuracy ---------------------------------------------------


def test_every_probe_caps_its_own_concurrency():
    """A refused request becomes a false finding about the client's site.

    Measured on prosperitymedia.com.au: `probe_site` and `probe_tech` fired
    together put fourteen requests at one shared-hosting WordPress site, four
    were refused, and all four agent surfaces were reported `unreachable` while
    a slower pass showed them honestly 404ing. The endpoint count went from five
    to one at the same time, thinning the generated file.
    """
    import inspect

    from app.scrape import agents_probe, readiness, tech_probe

    for module in (agents_probe, tech_probe, readiness):
        assert hasattr(module, "MAX_CONCURRENCY"), module.__name__
        assert module.MAX_CONCURRENCY <= 4, module.__name__
        source = inspect.getsource(module)
        assert "Semaphore" in source, module.__name__


def test_the_two_probes_are_not_raced_against_one_host():
    """Capping each and then firing both at once puts the total back up."""
    import inspect

    from app import main

    source = inspect.getsource(main._agents_document)
    assert "probe = await probe_site(" in source
    assert "asyncio.gather(\n        probe_site" not in source
