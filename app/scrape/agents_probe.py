"""What agent-facing surfaces a site actually publishes.

Everything the generator is allowed to claim comes from here. An `agents.md` is an
instruction manual an agent will *act* on: telling one to `POST /api/ucp/mcp` on a
site with no such endpoint is not an untidy description, it is an instruction that
fails, and the client's site looks broken to every assistant that reads the file.
So the rule is absolute -- a capability reaches the output only if a probe returned
both the right status and the right content type.

**A 200 is not evidence.** `vercel.com/agents.md` answers 200 with
`text/html`: a single-page-app shell, not a file. Measured 2026-08-20, alongside
`prosperitymedia.com.au`, `carsguide.com.au` and `stripe.com`, which all 404
honestly. Soft-404 is the common case rather than an edge one, and treating it as
presence would have us "audit" a React bundle. `discover.fetch_robots` already
carries this guard for robots.txt; this is the same check applied to four more
surfaces.

Two things are deliberately kept apart. `agents.md` is prose for agents and has no
formal specification -- it is a Shopify convention, shipped to every store in May
2026. UCP is the machine contract, governed at ucp.dev, discovered at
`/.well-known/ucp`. The prose file points at UCP; it is not part of it. Conflating
them would let us present a convention as a standard.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from enum import StrEnum

import httpx

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 20.0
# Small sites refuse a burst. Two at a time still finishes in about a second.
MAX_CONCURRENCY = 2

# Paths probed on every site.
AGENTS_PATH = "/agents.md"
UCP_PATH = "/.well-known/ucp"
LLMS_PATH = "/llms.txt"
LLMS_FULL_PATH = "/llms-full.txt"


class SurfaceState(StrEnum):
    """What we know about one surface. Four states, not two.

    `SOFT_404` and `UNREACHABLE` both mean "no usable file", and collapsing either
    into `ABSENT` would report a guess as a finding: one says the site answers with
    something that is not a file, the other says we could not ask.
    """

    PRESENT = "present"
    SOFT_404 = "soft_404"
    ABSENT = "absent"
    WRONG_TYPE = "wrong_type"
    UNREACHABLE = "unreachable"

    @property
    def usable(self) -> bool:
        return self is SurfaceState.PRESENT

    @property
    def is_evidence(self) -> bool:
        """Whether this state supports a claim about the site.

        `UNREACHABLE` does not. A network failure is a fact about us, not about the
        client's site, and must never be reported as "they do not publish one".
        """
        return self is not SurfaceState.UNREACHABLE


# Content types that count as a markdown-ish document. A file served as
# `text/html` is a page, whatever its path says.
TEXT_TYPES = ("text/markdown", "text/plain", "text/x-markdown", "application/markdown")
JSON_TYPES = ("application/json", "application/ld+json")


@dataclass(frozen=True, slots=True)
class Surface:
    """One probed URL and what came back."""

    url: str
    state: SurfaceState
    status: int | None = None
    content_type: str = ""
    body: str = ""
    detail: str = ""

    @property
    def usable(self) -> bool:
        return self.state.usable

    def describe(self) -> str:
        """A line for the operator that names the cause, not just the outcome."""
        match self.state:
            case SurfaceState.PRESENT:
                return f"{self.url} — published ({self.content_type or 'no content type'})"
            case SurfaceState.SOFT_404:
                return (
                    f"{self.url} — answers {self.status} with HTML, which is a page and "
                    "not a file. Treated as not published."
                )
            case SurfaceState.WRONG_TYPE:
                return (
                    f"{self.url} — served as {self.content_type!r}; agents expect "
                    "text/markdown or text/plain."
                )
            case SurfaceState.ABSENT:
                return f"{self.url} — not published ({self.status})"
            case _:
                return f"{self.url} — could not be checked: {self.detail}"


@dataclass(frozen=True, slots=True)
class UcpService:
    """One transport binding advertised by a UCP profile."""

    name: str
    version: str
    transport: str
    endpoint: str
    spec: str = ""


@dataclass(frozen=True, slots=True)
class UcpProfile:
    """The parsed `/.well-known/ucp` document.

    Every field is read from the document. Nothing is defaulted into existence: a
    profile with no MCP service yields no MCP endpoint, and the generator then has
    nothing to say about MCP, which is the correct outcome.
    """

    version: str = ""
    supported_versions: tuple[str, ...] = ()
    services: tuple[UcpService, ...] = ()

    @property
    def mcp_endpoints(self) -> tuple[str, ...]:
        return tuple(s.endpoint for s in self.services if s.transport == "mcp" and s.endpoint)


@dataclass(slots=True)
class ProbeResult:
    """Everything verified about a site's agent-facing surfaces."""

    site_url: str
    agents_md: Surface | None = None
    ucp: Surface | None = None
    llms_txt: Surface | None = None
    llms_full: Surface | None = None
    ucp_profile: UcpProfile | None = None
    platform: str = "unknown"
    notes: list[str] = field(default_factory=list)

    @property
    def has_agents_md(self) -> bool:
        return bool(self.agents_md and self.agents_md.usable)

    @property
    def has_ucp(self) -> bool:
        return bool(self.ucp and self.ucp.usable and self.ucp_profile)

    @property
    def is_shopify(self) -> bool:
        return self.platform == "shopify"

    @property
    def verified_endpoints(self) -> tuple[str, ...]:
        """The only endpoints the generator may name."""
        return self.ucp_profile.mcp_endpoints if self.ucp_profile else ()

    def surfaces(self) -> list[Surface]:
        return [s for s in (self.agents_md, self.ucp, self.llms_txt, self.llms_full) if s]

    def summary(self) -> str:
        return "\n".join(surface.describe() for surface in self.surfaces())


def _classify(response: httpx.Response, expected: tuple[str, ...]) -> tuple[SurfaceState, str]:
    """Turn a response into a state, with the content type doing most of the work."""
    content_type = (response.headers.get("content-type") or "").split(";")[0].strip().lower()

    if response.status_code == 404:
        return SurfaceState.ABSENT, ""
    if response.status_code >= 400:
        return SurfaceState.ABSENT, f"status {response.status_code}"
    if "html" in content_type:
        # The soft-404. A framework catch-all route answers 200 for any path, so
        # the status says nothing and the content type says everything.
        return SurfaceState.SOFT_404, content_type
    if expected and content_type and content_type not in expected:
        return SurfaceState.WRONG_TYPE, content_type
    return SurfaceState.PRESENT, content_type


async def _probe(
    client: httpx.AsyncClient, site_url: str, path: str, expected: tuple[str, ...]
) -> Surface:
    url = site_url.rstrip("/") + path
    try:
        response = await client.get(url)
    except httpx.HTTPError as exc:
        # Distinguished from absence on purpose: we could not ask.
        return Surface(url=url, state=SurfaceState.UNREACHABLE, detail=f"{type(exc).__name__}")

    state, content_type = _classify(response, expected)
    body = response.text if state is SurfaceState.PRESENT else ""
    return Surface(
        url=url,
        state=state,
        status=response.status_code,
        content_type=content_type,
        body=body[:200_000],
    )


def parse_ucp(body: str) -> UcpProfile | None:
    """Read a UCP profile, or return None rather than a half-built one.

    Shaped against the live document at `allbirds.com/.well-known/ucp`, which nests
    everything under a `ucp` key and lists services keyed by reverse-domain
    capability name -- `dev.ucp.shopping` -- each holding a list of transport
    bindings. A malformed document is treated as no document: a partially parsed
    profile would let an invented endpoint through, which is the one thing this
    module exists to prevent.
    """
    try:
        document = json.loads(body)
    except (ValueError, TypeError):
        return None
    if not isinstance(document, dict):
        return None

    root = document.get("ucp") if isinstance(document.get("ucp"), dict) else document
    if not isinstance(root, dict):
        return None

    services: list[UcpService] = []
    raw_services = root.get("services")
    if isinstance(raw_services, dict):
        for name, bindings in raw_services.items():
            if not isinstance(bindings, list):
                continue
            for binding in bindings:
                if not isinstance(binding, dict):
                    continue
                endpoint = str(binding.get("endpoint") or "")
                if not endpoint:
                    continue
                services.append(
                    UcpService(
                        name=str(name),
                        version=str(binding.get("version") or ""),
                        transport=str(binding.get("transport") or ""),
                        endpoint=endpoint,
                        spec=str(binding.get("spec") or ""),
                    )
                )

    supported = root.get("supported_versions")
    supported_versions = tuple(sorted(supported)) if isinstance(supported, dict) else ()

    version = str(root.get("version") or "")
    if not (version or services):
        return None

    return UcpProfile(
        version=version, supported_versions=supported_versions, services=tuple(services)
    )


def detect_platform(result: ProbeResult, headers: dict[str, str] | None = None) -> str:
    """Name the platform, from evidence rather than from HTML fingerprinting.

    A UCP endpoint on `*.myshopify.com` is conclusive -- Shopify writes its own
    profile and cannot be impersonated by a theme. Response headers are the
    fallback. HTML sniffing is deliberately not used: it is the least reliable
    signal and this decision changes what artefact the operator is handed.
    """
    if result.ucp_profile:
        for service in result.ucp_profile.services:
            if "myshopify.com" in service.endpoint:
                return "shopify"
    lowered = {k.lower(): v.lower() for k, v in (headers or {}).items()}
    if any("shopify" in value for value in lowered.values()):
        return "shopify"
    if "x-powered-by" in lowered and "wordpress" in lowered["x-powered-by"]:
        return "wordpress"
    return "unknown"


async def probe_site(
    site_url: str, user_agent: str, timeout: float = DEFAULT_TIMEOUT
) -> ProbeResult:
    """Check every agent-facing surface a site might publish."""
    origin = site_url.rstrip("/")
    result = ProbeResult(site_url=origin)

    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=timeout,
        headers={"User-Agent": user_agent, "Accept": "*/*"},
    ) as client:
        # Capped, for the same reason the readiness audit is: a shared-hosting
        # WordPress site refused four of these when they arrived together with
        # the technology probe's ten, and every refusal was then reported as
        # "this site does not publish one". Measured on prosperitymedia.com.au,
        # where all four surfaces came back unreachable while a slower pass
        # showed llms.txt honestly 404ing. Our own impatience is not evidence.
        gate = asyncio.Semaphore(MAX_CONCURRENCY)

        async def limited(path: str, expected: tuple[str, ...]) -> Surface:
            async with gate:
                return await _probe(client, origin, path, expected)

        agents_md, ucp, llms_txt, llms_full = await asyncio.gather(
            limited(AGENTS_PATH, TEXT_TYPES),
            limited(UCP_PATH, JSON_TYPES),
            limited(LLMS_PATH, TEXT_TYPES),
            limited(LLMS_FULL_PATH, TEXT_TYPES),
        )
        headers: dict[str, str] = {}
        try:
            root = await client.get(origin + "/")
            headers = dict(root.headers)
        except httpx.HTTPError:
            pass

    result.agents_md = agents_md
    result.ucp = ucp
    result.llms_txt = llms_txt
    result.llms_full = llms_full

    if ucp.usable:
        result.ucp_profile = parse_ucp(ucp.body)
        if result.ucp_profile is None:
            result.notes.append(
                f"{ucp.url} returned JSON that is not a UCP profile; no endpoints taken from it."
            )

    result.platform = detect_platform(result, headers)

    for surface in result.surfaces():
        # Both are worth telling the operator about, for opposite reasons: a
        # soft-404 is a site answering misleadingly, and unreachable is us failing
        # to ask. Neither is a plain absence, and a silent probe would let both
        # read as one.
        if surface.state in (SurfaceState.SOFT_404, SurfaceState.UNREACHABLE):
            result.notes.append(surface.describe())

    return result
