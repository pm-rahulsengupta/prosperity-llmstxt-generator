"""Turning a probe into a stored row and back again.

Every site page used to re-probe the client's site on GET. Serving them from a
stored row needs the three probe objects to survive a round trip through JSONB,
and that codec lives here rather than in the routes so there is one definition of
what a snapshot contains -- a second copy is how the stored shape and the read
shape drift apart, and a drifted codec fails by returning a plausible object with
the wrong values in it rather than by raising.

Two rules the round trip must hold, both of which have their own test:

**A component is referenced by key, never serialised.** `app/core/components.py`
is the registry, and writing a copy of a `Component` into every snapshot row
would mean a registry edit silently disagreeing with thousands of stored rows.
A key that no longer resolves is dropped on read, which is the correct outcome
for a component that has since been removed.

**`None` and absent stay distinct.** `Surface.status` of `None` means no
response; `0` would mean a response of zero. The codec never defaults one into
the other, which is the same evidence rule the probes themselves follow.
"""

from __future__ import annotations

from app.core.bundle import DeclaredEndpoint
from app.core.components import by_key
from app.scrape.agents_probe import ProbeResult, Surface, SurfaceState, UcpProfile, UcpService
from app.scrape.readiness import CheckResult, CheckState, ReadinessReport, SiteType
from app.scrape.tech_probe import Detection, Platform, TechProfile

__all__ = [
    "declared_from_list",
    "declared_to_list",
    "probe_from_dict",
    "probe_to_dict",
    "readiness_from_dict",
    "readiness_to_dict",
    "tech_from_dict",
    "tech_to_dict",
]


# -- probe -------------------------------------------------------------------


def _surface_to_dict(surface: Surface) -> dict:
    return {
        "url": surface.url,
        "state": surface.state.value,
        "status": surface.status,
        "content_type": surface.content_type,
        "detail": surface.detail,
        # `body` is deliberately dropped. It is the whole llms.txt of a large
        # site, it is never read back by any page, and storing it would make the
        # snapshot row grow with the client's content.
    }


def _surface_from_dict(raw: dict | None) -> Surface | None:
    if not raw:
        return None
    return Surface(
        url=raw.get("url", ""),
        state=SurfaceState(raw.get("state", SurfaceState.UNREACHABLE.value)),
        status=raw.get("status"),
        content_type=raw.get("content_type", ""),
        detail=raw.get("detail", ""),
    )


def probe_to_dict(probe: ProbeResult) -> dict:
    profile = probe.ucp_profile
    return {
        "site_url": probe.site_url,
        "agents_md": _surface_to_dict(probe.agents_md) if probe.agents_md else None,
        "ucp": _surface_to_dict(probe.ucp) if probe.ucp else None,
        "llms_txt": _surface_to_dict(probe.llms_txt) if probe.llms_txt else None,
        "llms_full": _surface_to_dict(probe.llms_full) if probe.llms_full else None,
        "platform": probe.platform,
        "notes": list(probe.notes),
        "ucp_profile": None
        if profile is None
        else {
            "version": profile.version,
            "supported_versions": list(profile.supported_versions),
            "services": [
                {
                    "name": s.name,
                    "version": s.version,
                    "transport": s.transport,
                    "endpoint": s.endpoint,
                    "spec": s.spec,
                }
                for s in profile.services
            ],
        },
    }


def probe_from_dict(raw: dict) -> ProbeResult:
    profile = raw.get("ucp_profile")
    return ProbeResult(
        site_url=raw.get("site_url", ""),
        agents_md=_surface_from_dict(raw.get("agents_md")),
        ucp=_surface_from_dict(raw.get("ucp")),
        llms_txt=_surface_from_dict(raw.get("llms_txt")),
        llms_full=_surface_from_dict(raw.get("llms_full")),
        platform=raw.get("platform", "unknown"),
        notes=list(raw.get("notes") or []),
        ucp_profile=None
        if not profile
        else UcpProfile(
            version=profile.get("version", ""),
            supported_versions=tuple(profile.get("supported_versions") or ()),
            services=tuple(
                UcpService(
                    name=s.get("name", ""),
                    version=s.get("version", ""),
                    transport=s.get("transport", ""),
                    endpoint=s.get("endpoint", ""),
                    spec=s.get("spec", ""),
                )
                for s in (profile.get("services") or [])
            ),
        ),
    )


# -- readiness ---------------------------------------------------------------


def readiness_to_dict(report: ReadinessReport) -> dict:
    return {
        "site_url": report.site_url,
        "site_type": report.site_type.value,
        "sampled": list(report.sampled),
        "results": [
            {
                "key": result.item.key,
                "state": result.state.value,
                "detail": result.detail,
                "url": result.url,
            }
            for result in report.results
        ],
    }


def readiness_from_dict(raw: dict) -> ReadinessReport:
    """Rebuild the report, resolving each result back to the live registry.

    A key that no longer resolves is dropped rather than faked. The score is a
    property over `results`, so dropping a retired component simply removes it
    from both halves of the fraction -- which is what should happen to a check
    the tool no longer performs.
    """
    report = ReadinessReport(
        site_url=raw.get("site_url", ""),
        site_type=SiteType(raw.get("site_type", SiteType.CONTENT.value)),
    )
    report.sampled = list(raw.get("sampled") or [])
    for entry in raw.get("results") or []:
        component = by_key(entry.get("key", ""))
        if component is None:
            continue
        report.results.append(
            CheckResult(
                component,
                CheckState(entry.get("state", CheckState.UNREACHABLE.value)),
                entry.get("detail", ""),
                entry.get("url", ""),
            )
        )
    return report


# -- declared endpoints ------------------------------------------------------


def declared_to_list(declared: list[DeclaredEndpoint]) -> list[dict]:
    """The operator's own endpoints and whether each answered.

    Stored with the probe rather than recomputed on render, because verifying
    them is itself a network call per endpoint -- the cost this whole table
    exists to take off the request path.
    """
    return [
        {"kind": d.kind, "url": d.url, "verified": d.verified, "detail": d.detail} for d in declared
    ]


def declared_from_list(raw: list | None) -> list[DeclaredEndpoint]:
    """`verified` defaults to False, never True.

    The invariant the whole tool rests on: a capability is claimed only where a
    probe confirmed it. A malformed row must fail closed.
    """
    return [
        DeclaredEndpoint(
            kind=d.get("kind", ""),
            url=d.get("url", ""),
            verified=bool(d.get("verified", False)),
            detail=d.get("detail", ""),
        )
        for d in (raw or [])
    ]


# -- tech --------------------------------------------------------------------


def _detections_to_list(detections: list[Detection]) -> list[dict]:
    return [{"name": d.name, "evidence": d.evidence, "url": d.url} for d in detections]


def _detections_from_list(raw: list | None) -> list[Detection]:
    return [
        Detection(name=d.get("name", ""), evidence=d.get("evidence", ""), url=d.get("url", ""))
        for d in (raw or [])
    ]


def tech_to_dict(tech: TechProfile) -> dict:
    return {
        "site_url": tech.site_url,
        "platform": tech.platform.value,
        "platform_evidence": tech.platform_evidence,
        "endpoints": _detections_to_list(tech.endpoints),
        "signals": _detections_to_list(tech.signals),
        "notes": list(tech.notes),
        "technologies": list(tech.technologies),
        "ecommerce_tech": list(tech.ecommerce_tech),
    }


def tech_from_dict(raw: dict) -> TechProfile:
    return TechProfile(
        site_url=raw.get("site_url", ""),
        platform=Platform(raw.get("platform", Platform.UNKNOWN.value)),
        platform_evidence=raw.get("platform_evidence", ""),
        endpoints=_detections_from_list(raw.get("endpoints")),
        signals=_detections_from_list(raw.get("signals")),
        notes=list(raw.get("notes") or []),
        technologies=list(raw.get("technologies") or []),
        ecommerce_tech=list(raw.get("ecommerce_tech") or []),
    )
