"""What a client may see. Nothing else is on these objects.

Built from a `SiteView` and a `SiteStatus`, both of which carry rule ids, verify
commands, artifact names, operator email addresses and probe plumbing. None of
that survives the crossing. This module is the boundary, and it is a boundary
made of dataclass fields rather than of `{% if %}` in a template, because a
template guard is one careless edit away from being deleted and a missing field
is not. A leaked guard also fails silently, as perfectly valid HTML.

The leak that made this necessary is real, not hypothetical. `site_state.derive`
writes ``f"marked done by {marks[key]}"`` into `ComponentStatus.detail`, where
the value is the operator's email address, and `partials/component.html` renders
`detail` unconditionally. Rendering the staff partial for a client would put a
Prosperity Media address in a client's PDF.

Pure: no database, no network, no Jinja. Every field below is a `str`, `int`,
`bool`, `date`, `tuple` or another frozen dataclass from this module -- a
template cannot reach `.component.key` because there is no `.component`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from app.core.components import (
    EFFORT_LABELS,
    EFFORT_OWNERS,
    FAMILY_BLURBS,
    FAMILY_LABELS,
    ComponentState,
    Family,
)

__all__ = [
    "SECTION_KEYS",
    "ClientCheck",
    "ClientFile",
    "ClientFinding",
    "ClientGroup",
    "ClientItem",
    "ClientReport",
    "ClientSection",
    "ClientStat",
    "Standing",
    "build_client_report",
    "section_title",
]


class Standing(StrEnum):
    """The three states a client can act on.

    Five internal states collapse to three. `not_applicable` is not among them:
    those items are dropped rather than labelled, for the reason the family tab
    already gives -- a page of "does not apply" reads as a broken tool rather
    than as a decision. `ready` and `template` both mean "not live yet", but they
    differ in whether we have something to hand over, which is the only part of
    that distinction a client will act on.
    """

    DONE = "done"
    PREPARED = "prepared"
    OUTSTANDING = "outstanding"


STANDING_LABELS: dict[Standing, str] = {
    Standing.DONE: "In place",
    Standing.PREPARED: "Prepared for you",
    Standing.OUTSTANDING: "Not in place",
}

#: Reuses the pill classes already in main.css rather than inventing a second
#: palette, so a client document cannot drift from the brand the staff pages use.
STANDING_TONES: dict[Standing, str] = {
    Standing.DONE: "ok",
    Standing.PREPARED: "wait",
    Standing.OUTSTANDING: "bad",
}

_STANDING_OF: dict[ComponentState, Standing] = {
    ComponentState.LIVE: Standing.DONE,
    ComponentState.READY: Standing.PREPARED,
    ComponentState.TEMPLATE: Standing.PREPARED,
    ComponentState.MISSING: Standing.OUTSTANDING,
}

#: A client is told what to do next, in their own terms. `derive`'s own strings
#: ("generated and ready to publish") are internal bookkeeping written for us.
#: Nothing for DONE: the pill already says "In place", and repeating "nothing to
#: do" under every one of them is a column of noise a reader learns to skip --
#: which is a bad habit to teach in a document whose other rows matter.
_WHAT_TO_DO: dict[Standing, str] = {
    Standing.DONE: "",
    Standing.PREPARED: "We have prepared this for you. It needs publishing.",
    Standing.OUTSTANDING: "This needs building. It is not something we can generate for you.",
}

#: The bands documented in `rules/registry.py` where the severity caps are
#: applied, named rather than restated so the taxonomy has one home.
_BANDS: tuple[tuple[int, str, str], ...] = (
    (90, "Ready to publish", "ok"),
    (80, "Minor issues", "wait"),
    (50, "Needs attention", "wait"),
    (0, "Needs work", "bad"),
)

#: Three examples is enough to recognise a pattern; more is a wall of quoted
#: markup in a document meant to be read.
MAX_EXAMPLES = 3
#: Findings beyond this are counted, never silently dropped -- a short clean list
#: over a long dirty one is the inflation the rule engine exists to prevent.
MAX_FINDINGS = 8

SECTION_KEYS: tuple[str, ...] = (
    "overview",
    "checklist",
    "handover",
    "files",
    *(family.value for family in Family),
    "report",
)

#: What the combined report contains. Deliberately not the six families:
#: `for_client()` and `for_developer()` partition the applicable set exactly, so
#: this covers every component once. Overview plus six families would list every
#: item twice -- once under its family and once as work -- which reads as a bug
#: in a twenty-page document.
REPORT_SECTIONS: tuple[str, ...] = ("overview", "checklist", "handover", "files")

_TITLES: dict[str, str] = {
    "overview": "Where the site stands",
    "checklist": "What you can do",
    "handover": "For your developer",
    "files": "Files to publish",
    "report": "AI search readiness review",
}


def section_title(section: str) -> str:
    if section in _TITLES:
        return _TITLES[section]
    return FAMILY_LABELS[Family(section)]


@dataclass(frozen=True, slots=True)
class ClientFinding:
    """One thing wrong with a file we generated, in words.

    `Finding.rule_id` is deliberately absent. "FULL-003" is an index into a rule
    table the client cannot read and would have to ask us about, and carrying it
    would make the document a map of our rule set, which is ours. The `message`
    is already the sentence we would say back to them.
    """

    message: str
    count: int = 0
    examples: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ClientCheck:
    """How a generated file measured up, as a band rather than a number.

    No `82/100`. A number invites "why not 100?", and the honest answer is a list
    of rule identifiers that have just been removed for being unreadable.
    """

    band: str
    tone: str
    findings: tuple[ClientFinding, ...] = ()
    withheld: int = 0


@dataclass(frozen=True, slots=True)
class ClientItem:
    title: str
    standing: Standing
    standing_label: str
    tone: str
    why: str = ""
    evidence: str = ""
    what_to_do: str = ""
    file_name: str = ""
    serve_at: str = ""
    work_label: str = ""
    who: str = ""
    how_to_check: str = ""
    #: What to actually do, in order. Rendered behind a disclosure so the list
    #: stays scannable and the detail is there for whoever does the work.
    steps: tuple[str, ...] = ()
    check: ClientCheck | None = None


@dataclass(frozen=True, slots=True)
class ClientGroup:
    """A run of items under an optional heading.

    Exists so the handover, which groups by effort, and everything else, which is
    flat, are the same shape: a flat section is one group with no title. One
    template renders both, so there is no second code path to keep in step.
    """

    title: str = ""
    note: str = ""
    items: tuple[ClientItem, ...] = ()


@dataclass(frozen=True, slots=True)
class ClientFile:
    """One generated file, and whether it is on the site yet.

    The tick is **derived from the probe, not from anyone's memory**. Every other
    export checklist in this trade is a list somebody has to keep in their head
    or in a spreadsheet; this one already knows, because the same check that
    decides a component is LIVE is what fetched the URL. A row that says
    "published" is a row where we asked the site and it answered.
    """

    name: str
    serve_at: str
    size: str
    published: bool
    state_label: str
    tone: str
    note: str = ""
    steps: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ClientStat:
    label: str
    value: str


@dataclass(frozen=True, slots=True)
class ClientSection:
    key: str
    kind: str
    title: str
    blurb: str = ""
    intro: str = ""
    stats: tuple[ClientStat, ...] = ()
    rows: tuple[tuple[str, str, str], ...] = ()
    groups: tuple[ClientGroup, ...] = ()
    files: tuple[ClientFile, ...] = ()
    empty_note: str = ""


@dataclass(frozen=True, slots=True)
class ClientReport:
    domain: str
    site_url: str
    client_name: str
    title: str
    prepared_on: date
    # Formatted here rather than in the template. `%-d` is glibc-only and breaks
    # on Windows, and the alternatives are all string surgery in Jinja.
    prepared_label: str = ""
    checked_ago: str = ""
    is_stale: bool = False
    sections: tuple[ClientSection, ...] = ()


# -- the three rules that do the stripping ------------------------------------


def _evidence(status) -> str:
    """A probe's own words about the client's own site, or nothing.

    One predicate closes three separate leaks, which is why it is a predicate and
    not three checks:

    * `derive` writes "marked done by <operator email>" for a manual mark, and a
      mark is by construction never probe-decided.
    * Where a check came back MANUAL, `derive` puts the **verify command** in
      `detail`. "npx lighthouse <site> --view" is not evidence, it is an
      instrument, and MANUAL is neither PASS nor FAIL so it is not decided.
    * The fallback strings -- "generated and ready to publish", "not expected on
      a b2b/saas site" -- are written when no check ran at all. They are our
      bookkeeping; the client gets `what_to_do`, which is written for them.

    What survives is exactly a PASS or FAIL detail: "200, text/plain, 1.4 KB",
    "404". Facts about their site, in a probe's voice.
    """
    return status.detail if status.probe_decided else ""


def _band(report) -> tuple[str, str]:
    score = getattr(report, "score", 0)
    for floor, label, tone in _BANDS:
        if score >= floor:
            return label, tone
    return "Needs work", "bad"


def _check_of(report) -> ClientCheck | None:
    if report is None:
        return None
    failures = list(getattr(report, "failures", ()))
    band, tone = _band(report)
    shown = failures[:MAX_FINDINGS]
    findings = tuple(
        ClientFinding(
            message=str(getattr(f, "message", "")),
            count=int(getattr(f, "count", 0) or 0),
            examples=tuple(str(e) for e in list(getattr(f, "examples", ()))[:MAX_EXAMPLES]),
        )
        for f in shown
    )
    return ClientCheck(
        band=band, tone=tone, findings=findings, withheld=max(0, len(failures) - len(shown))
    )


def _publish_steps(name: str, serve_at: str, site_url: str, note: str, task) -> tuple[str, ...]:
    """How to put one generated file on a site.

    Assembled rather than written per component. Twenty-one hand-written guides
    would be twenty-one things to keep true; these are built from the artifact's
    own path, the site's own URL, and the `DeploymentTask` the bundle already
    produces -- including its platform hint, which is why a Shopify client is told
    something different from a WordPress one.

    `bundle.tasks` was generated for every run and rendered by no page. This is
    where it finally reaches a reader.
    """
    steps = [
        f"Download {name} from this document.",
        f"Upload it to your site so it answers at {site_url.rstrip('/')}{serve_at}.",
    ]
    if note:
        steps.append(note)
    hint = getattr(task, "platform_hint", "") if task is not None else ""
    if hint and hint != note:
        steps.append(hint)
    blocked = getattr(task, "blocked_by", "") if task is not None else ""
    if blocked:
        steps.append(f"This cannot go live until it is resolved: {blocked}.")
    steps.append(f"Open {site_url.rstrip('/')}{serve_at} in a browser to confirm it is there.")
    return tuple(steps)


def _work_steps(status, task) -> tuple[str, ...]:
    """How to do a change that produces no file.

    Link headers, content negotiation and WebMCP cannot be handed over as a
    download -- they are changes to how the site answers. `_deployment_tasks`
    already reasons about the platform for each; this renders that reasoning.
    """
    steps: list[str] = []
    what = getattr(task, "what", "") if task is not None else ""
    if what:
        steps.append(what)
    hint = getattr(task, "platform_hint", "") if task is not None else ""
    if hint and hint != what:
        steps.append(hint)
    blocked = getattr(task, "blocked_by", "") if task is not None else ""
    if blocked:
        steps.append(f"Blocked until this is resolved: {blocked}.")
    if status.component.expect:
        steps.append(f"What good looks like: {status.component.expect}")
    return tuple(steps)


def _item(
    status,
    *,
    report=None,
    with_verify: bool = False,
    tasks: Mapping[str, object] | None = None,
    notes: Mapping[str, str] | None = None,
    site_url: str = "",
) -> ClientItem:
    standing = _STANDING_OF.get(status.state, Standing.OUTSTANDING)
    component = status.component
    tasks = tasks or {}
    artifact = status.artifact_name or ""
    task = tasks.get(artifact) or tasks.get(component.title)
    if standing is Standing.DONE:
        # Nothing to instruct. A guide under an item that is already working is
        # a paragraph inviting someone to change something that works.
        steps: tuple[str, ...] = ()
    elif artifact:
        steps = _publish_steps(
            artifact,
            component.path or f"/{artifact}",
            site_url,
            (notes or {}).get(artifact, ""),
            task,
        )
    else:
        steps = _work_steps(status, task)
    return ClientItem(
        title=component.title,
        standing=standing,
        standing_label=STANDING_LABELS[standing],
        tone=STANDING_TONES[standing],
        why=component.why or "",
        evidence=_evidence(status),
        what_to_do=_WHAT_TO_DO[standing],
        # Named, never linked: `/agents/download` is behind `require_user` and
        # would bounce a client to a sign-in page. The share route serves files
        # through the token instead. `template_body` is dropped outright -- it is
        # scaffolding stamped "Not for publication", and handing a client a file
        # that says so invites exactly one conversation.
        file_name=status.artifact_name or "",
        serve_at=(component.path or status.artifact_name or "") if status.artifact_name else "",
        work_label=EFFORT_LABELS[component.effort] if component.needs_developer else "",
        who=EFFORT_OWNERS[component.effort] if component.needs_developer else "",
        # Only in the handover, whose declared reader is the client's developer.
        # "DevTools > Elements > Accessibility tab" in a marketing director's PDF
        # reads as a document meant for somebody else.
        how_to_check=(status.verify or "") if with_verify else "",
        steps=steps,
        check=_check_of(report),
    )


# -- sections ------------------------------------------------------------------


def _visible(statuses: Sequence) -> list:
    return [s for s in statuses if s.state is not ComponentState.NOT_APPLICABLE]


def _flat(items: Sequence[ClientItem]) -> tuple[ClientGroup, ...]:
    return (ClientGroup(items=tuple(items)),) if items else ()


def _overview(view, status) -> ClientSection:
    readiness = view.readiness
    stats = [
        ClientStat("Readiness", f"{readiness.score}/100" if readiness else "not measured"),
        ClientStat("In place", f"{status.live_count} of {status.applicable_count}"),
    ]
    rows = tuple(
        (
            label,
            f"{counts['live']} of {counts['total']} in place",
            "ok" if counts["live"] == counts["total"] else "wait",
        )
        for _family, label, counts in status.family_counts()
    )
    return ClientSection(
        key="overview",
        kind="summary",
        title=_TITLES["overview"],
        blurb="How ready this site is for AI search and agent traffic.",
        intro=(
            "Each group below is a part of the site an assistant reads differently. "
            "The detail follows."
        ),
        stats=tuple(stats),
        rows=rows,
        empty_note="This site has not been checked yet.",
    )


def _checklist(view, status, reports, ctx) -> ClientSection:
    items = [_item(s, report=reports.get(s.key), **ctx) for s in _visible(status.for_client())]
    done = sum(1 for i in items if i.standing is Standing.DONE)
    return ClientSection(
        key="checklist",
        kind="list",
        title=_TITLES["checklist"],
        blurb="Work that needs no developer.",
        intro=f"{done} of {len(items)} already in place." if items else "",
        groups=_flat(items),
        empty_note="Everything on this site needs a developer. See the next section.",
    )


def _handover(view, status, reports, ctx) -> ClientSection:
    groups = tuple(
        ClientGroup(
            title=EFFORT_LABELS[effort],
            note=EFFORT_OWNERS[effort],
            items=tuple(
                _item(s, report=reports.get(s.key), with_verify=True, **ctx)
                for s in _visible(statuses)
            ),
        )
        for effort, statuses in status.by_effort().items()
        if _visible(statuses)
    )
    return ClientSection(
        key="handover",
        kind="list",
        title=_TITLES["handover"],
        blurb="Work that needs someone with access to the site's code or hosting.",
        intro="Grouped by who does it, so nobody is blocked waiting on somebody else.",
        groups=groups,
        empty_note="Nothing here needs a developer.",
    )


def _family(view, status, family: Family, reports, ctx) -> ClientSection:
    items = [_item(s, report=reports.get(s.key), **ctx) for s in _visible(status.family(family))]
    return ClientSection(
        key=family.value,
        kind="list",
        title=FAMILY_LABELS[family],
        blurb=FAMILY_BLURBS[family],
        groups=_flat(items),
        # The wording the family tab already uses: this is a decision about the
        # kind of site, not a gap in the audit.
        empty_note="Nothing in this group applies to a site like this one.",
    )


def _size(body: str) -> str:
    """Rounded, and never "0 KB" for a file that exists.

    A file the client is about to publish is worth sizing so they can tell a
    two-line stub from a megabyte of full text before they upload it.
    """
    chars = len(body)
    if chars < 1024:
        return f"{chars} bytes"
    if chars < 1024 * 1024:
        return f"{chars / 1024:.0f} KB"
    return f"{chars / (1024 * 1024):.1f} MB"


def _files(view, status, tasks) -> ClientSection:
    """Every file we generated, in one list, with its published state.

    Built from `view.bundle.artifacts` rather than from the component registry,
    because the bundle is what actually exists: a component may be applicable and
    have produced nothing, and a list of files that includes files there are no
    files for is worse than no list.

    The published state comes from the component whose `artifact` matches, which
    is the same `SiteStatus` every other section reads -- so this list cannot
    claim something is live that a family tab says is missing.
    """
    # `component.artifact`, not `status.artifact_name`. `derive` sets
    # `artifact_name` only on a READY component -- the LIVE branch never does,
    # because a file already published is not one we are handing over. Matching on
    # `artifact_name` would therefore have made the published set permanently
    # empty and every row in this list read "not published yet", which is exactly
    # the confident-and-wrong output this tool exists to avoid.
    live_artifacts = {
        s.component.artifact
        for s in status.statuses
        if s.component.artifact and s.state is ComponentState.LIVE
    }
    files = tuple(
        ClientFile(
            name=artifact.name,
            serve_at=artifact.path or f"/{artifact.name}",
            size=_size(artifact.body),
            published=artifact.name in live_artifacts,
            state_label="Published" if artifact.name in live_artifacts else "Not published yet",
            tone="ok" if artifact.name in live_artifacts else "wait",
            note=artifact.note,
            steps=(
                ()
                if artifact.name in live_artifacts
                else _publish_steps(
                    artifact.name,
                    artifact.path or f"/{artifact.name}",
                    view.site_url,
                    artifact.note,
                    tasks.get(artifact.name),
                )
            ),
        )
        for artifact in view.bundle.artifacts
    )
    done = sum(1 for f in files if f.published)
    return ClientSection(
        key="files",
        kind="files",
        title=_TITLES["files"],
        blurb="Everything we generated for this site, and where each one belongs.",
        intro=(
            f"{done} of {len(files)} already live. Each row says where to put the file; "
            "the tick is not a promise, it is the result of asking the site."
            if files
            else ""
        ),
        files=files,
        empty_note="Nothing has been generated for this site yet.",
    )


def _section(view, status, key: str, reports, ctx) -> ClientSection:
    if key == "overview":
        return _overview(view, status)
    if key == "checklist":
        return _checklist(view, status, reports, ctx)
    if key == "handover":
        return _handover(view, status, reports, ctx)
    if key == "files":
        return _files(view, status, ctx["tasks"])
    return _family(view, status, Family(key), reports, ctx)


def build_client_report(
    view,
    status,
    section: str,
    *,
    client_name: str = "",
    prepared_on: date | None = None,
    reports: Mapping[str, object] | None = None,
) -> ClientReport:
    """Everything a client may see about one section, and nothing else.

    `reports` is injectable so tests can supply rule findings without building a
    bundle; passing `None` computes them from the view, which is what the routes
    do. `reports_for` is pure, so this stays pure.
    """
    if section not in SECTION_KEYS:
        raise ValueError(f"unknown section: {section!r}")

    if reports is None:
        from app.core.evidence import reports_for

        reports = reports_for(view)

    # `bundle.tasks` is generated on every run and, until now, rendered by no
    # page at all. It is where the platform-specific instructions live -- a
    # Shopify client and a WordPress one are told different things about Link
    # headers -- so the steps below are assembled from it rather than written out
    # per component and left to drift.
    tasks = {task.component: task for task in view.bundle.tasks}
    notes = {artifact.name: artifact.note for artifact in view.bundle.artifacts}
    ctx = {"tasks": tasks, "notes": notes, "site_url": view.site_url}

    keys = REPORT_SECTIONS if section == "report" else (section,)
    sections = tuple(_section(view, status, key, reports, ctx) for key in keys)

    return ClientReport(
        domain=view.domain,
        site_url=view.site_url,
        client_name=client_name or view.domain,
        title=section_title(section),
        prepared_on=(stamped := prepared_on or date.today()),
        prepared_label=f"{stamped.day} {stamped:%B %Y}",
        checked_ago=view.checked_ago,
        is_stale=view.is_stale,
        sections=sections,
    )
