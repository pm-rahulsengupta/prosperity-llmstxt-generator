"""What state each component is in for one site, derived fresh every time.

Nothing here is stored. The status of a component is a fact about the site as it
is right now, and a remembered tick is a claim about the site as it was when
somebody last looked — which is exactly the sort of quiet staleness this tool
spends its effort avoiding elsewhere.

That choice has one honest cost, taken deliberately: the six components needing a
rendered page or a CDN dashboard can never show as done, because nothing this
module can reach proves them. They appear as `MANUAL` with the command to run,
and they are excluded from the score rather than assumed either way.

Sits above `components` (the registry) and `readiness` (the prober), and below
the routes. It is the only place the four states are decided, so the family tabs,
the client checklist and the developer handover cannot disagree about what is
done — they are four renderings of one list.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.components import (
    COMPONENTS,
    Applicability,
    Component,
    ComponentState,
    Effort,
    Family,
    SiteType,
)
from app.scrape.readiness import STATIC_LAYER1, CheckState, ReadinessReport

__all__ = ["ComponentStatus", "SiteStatus", "derive"]


@dataclass(slots=True)
class ComponentStatus:
    """One component, its state, and the evidence for it."""

    component: Component
    state: ComponentState
    detail: str = ""
    # The generated file, where one exists and the scenario calls for it.
    artifact_name: str = ""
    # A starting point that must not be published. Mutually exclusive with the
    # artifact: a component is never both at once.
    template_body: str = ""
    verify: str = ""

    @property
    def key(self) -> str:
        return self.component.key

    @property
    def actionable(self) -> bool:
        """Whether there is something for a person to do about it."""
        return self.state in (ComponentState.READY, ComponentState.TEMPLATE, ComponentState.MISSING)

    @property
    def publishable(self) -> bool:
        """Only a real artefact. A template is never downloadable as final.

        The load-bearing property of the whole templating idea: it is checked
        wherever a file could leave the tool, so a placeholder cannot reach a
        client's web root by any route.
        """
        return self.state is ComponentState.READY and bool(self.artifact_name)


@dataclass(slots=True)
class SiteStatus:
    site_url: str
    site_type: SiteType
    statuses: list[ComponentStatus] = field(default_factory=list)

    def by_key(self, key: str) -> ComponentStatus | None:
        return next((s for s in self.statuses if s.key == key), None)

    def family(self, family: Family) -> list[ComponentStatus]:
        return [s for s in self.statuses if s.component.family is family]

    def for_client(self) -> list[ComponentStatus]:
        """Everything a person can action without a developer."""
        return [
            s
            for s in self.statuses
            if not s.component.needs_developer and s.state is not ComponentState.NOT_APPLICABLE
        ]

    def for_developer(self) -> list[ComponentStatus]:
        return [
            s
            for s in self.statuses
            if s.component.needs_developer and s.state is not ComponentState.NOT_APPLICABLE
        ]

    def by_effort(self) -> dict[Effort, list[ComponentStatus]]:
        grouped: dict[Effort, list[ComponentStatus]] = {e: [] for e in Effort}
        for status in self.for_developer():
            grouped[status.component.effort].append(status)
        return {effort: items for effort, items in grouped.items() if items}

    @property
    def live_count(self) -> int:
        return sum(1 for s in self.statuses if s.state is ComponentState.LIVE)

    @property
    def applicable_count(self) -> int:
        return sum(1 for s in self.statuses if s.state is not ComponentState.NOT_APPLICABLE)


def manually_markable(component: Component) -> bool:
    """Whether a person may assert this component is done.

    Only where no probe can settle it: layout shift, cursor styles, tap-target
    size, ghost overlays, WebMCP, Web Bot Auth. Everything else is decided by
    evidence, and letting someone tick `llms.txt` while it returns 404 would put
    a false claim in a client-facing report -- which is worse than the missing
    sense of progress that made manual marking worth adding.
    """
    return (component.layer == 1 and component.key not in STATIC_LAYER1_KEYS) or component.key in (
        "webmcp",
        "web-bot-auth",
    )


# Derived, not restated. This began as a hardcoded copy of the same three keys
# and immediately drifted: adding the WCAG 4.1.1 and 4.1.2 checks made them
# probe-decided in `readiness` and still hand-markable here, so someone could
# have ticked "no deprecated ARIA roles" on a page carrying one. Exactly the
# duplication the component registry exists to remove.
STATIC_LAYER1_KEYS = frozenset(STATIC_LAYER1)


def derive(
    site_url: str,
    site_type: SiteType,
    readiness: ReadinessReport | None = None,
    artifacts: dict[str, str] | None = None,
    templates: dict[str, str] | None = None,
    marks: dict[str, str] | None = None,
) -> SiteStatus:
    """Decide every component's state from the probe and what was generated.

    Order of precedence, and each step exists for a reason:

    1. **Not applicable** wins first. Scoring a law firm on its A2A agent card
       makes the number meaningless.
    2. **Live** next, from the probe. A component already published correctly
       needs nothing from us, and offering a replacement invites someone to
       overwrite a working file.
    3. **Ready** where we generated the artefact this scenario calls for.
    4. **Template** where we can only offer scaffolding.
    5. **Missing** for the rest — applicable, unpublished, and nothing we can
       produce, which is the honest description of a WebMCP integration nobody
       has built yet.
    """
    artifacts = artifacts or {}
    templates = templates or {}
    status = SiteStatus(site_url=site_url, site_type=site_type)

    checks = {r.item.key: r for r in (readiness.results if readiness else [])}

    for component in COMPONENTS:
        if component.applies_to(site_type) is Applicability.NO:
            status.statuses.append(
                ComponentStatus(
                    component,
                    ComponentState.NOT_APPLICABLE,
                    f"not expected on a {site_type.value.replace('_', '/')} site",
                    verify=component.verify,
                )
            )
            continue

        check = checks.get(component.key)
        if check is not None and check.state is CheckState.PASS:
            status.statuses.append(
                ComponentStatus(
                    component, ComponentState.LIVE, check.detail, verify=component.verify
                )
            )
            continue

        # A person's word, but only where nothing can check it for them, and
        # only until something can. If a manual item ever becomes probe-detectable
        # and the probe says no, the probe wins -- the check above runs first.
        marks = marks or {}
        if component.key in marks and manually_markable(component):
            status.statuses.append(
                ComponentStatus(
                    component,
                    ComponentState.LIVE,
                    f"marked done by {marks[component.key]}",
                    verify=component.verify,
                )
            )
            continue

        detail = check.detail if check is not None else ""
        if check is not None and check.state is CheckState.MANUAL:
            # Needs a rendered page or a dashboard. Never guessed at, in either
            # direction: the score excludes it and the command is the answer.
            detail = component.verify

        if component.artifact and component.artifact in artifacts:
            status.statuses.append(
                ComponentStatus(
                    component,
                    ComponentState.READY,
                    detail or "generated and ready to publish",
                    artifact_name=component.artifact,
                    verify=component.verify,
                )
            )
            continue

        if component.templated and component.key in templates:
            status.statuses.append(
                ComponentStatus(
                    component,
                    ComponentState.TEMPLATE,
                    detail or "a starting point exists; the service does not yet",
                    template_body=templates[component.key],
                    verify=component.verify,
                )
            )
            continue

        status.statuses.append(
            ComponentStatus(
                component,
                ComponentState.MISSING,
                detail or "not published, and nothing here can produce it",
                verify=component.verify,
            )
        )

    return status
