"""Joining an LLM Access Checker audit to the components this tool can fix.

The Checker is the diagnosis; this tool is the remediation. Its rubric is far
wider than anything here can generate, and the join has to make that visible
rather than imply the tool fixes everything:

    Schema & Entity        25%   no file we can produce
    Robots & Crawl         20%   robots.txt, Content-Signal
    JS Rendering           15%   no file we can produce
    AI Discoverability     15%   llms.txt, agents.md, .well-known/*
    AI Interactivity       15%   the templates, not the implementation
    Content & Citability   10%   no file we can produce

**35% of the weighted score maps onto files we generate; 65% does not.** The
second group is not dropped -- it becomes developer-handover work carrying the
Checker's own recommendation text, attributed to it.

**Attribution is absolute.** Nothing in here may render as something this tool
measured. That is the `probe_decided` rule from `site_state.py` applied to a
second source: an imported finding always arrives labelled with its origin and
the date the audit ran, because a stale third-party claim presented as our own
live check is the worst thing this integration could introduce.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.core.components import by_key

__all__ = [
    "AUDIT_PATHS",
    "GENERATED_PILLARS",
    "PATH_DISAGREEMENTS",
    "AuditFinding",
    "AuditView",
    "link_audit",
    "path_disagreements",
]

#: Checker probe path -> the component key it is evidence about.
#:
#: Path is the join key because both sides already speak it: the Checker's
#: `llm_result` raw data is keyed by the path it probed, and `Component.path`
#: is the same shape. Nothing here is inferred from wording.
AUDIT_PATHS: dict[str, str] = {
    "/robots.txt": "robots",
    "/llms.txt": "llms-txt",
    # Two variants the Checker probes and this tool does not. They are not
    # separate components -- they are the same file under names the ecosystem
    # has not settled, and finding one at any of them means the site has it.
    "/llm.txt": "llms-txt",
    "/.well-known/llm.txt": "llms-txt",
    "/llms-full.txt": "llms-full",
    "/agents.md": "agents-md",
    "/.well-known/ucp": "commerce-protocols",
    "/.well-known/agent-card.json": "a2a-card",
    "/.well-known/mcp.json": "mcp-card",
}

#: Where the two tools disagree about the location of the same file.
#:
#: Not resolved here on purpose. One of each pair is wrong, and picking a winner
#: inside a join function would hide a real conflict between two published
#: opinions -- whichever tool a client happened to run would decide where their
#: developer was told to put the file. Surfaced so a person settles it once.
PATH_DISAGREEMENTS: dict[str, tuple[str, str]] = {
    "a2a-card": ("/.well-known/agent-card.json", "/.well-known/agent.json"),
    "mcp-card": ("/.well-known/mcp.json", "/.well-known/mcp/server-card.json"),
}

#: Pillars whose findings this tool can answer with a generated file. Everything
#: else is developer work, and saying so is the point.
GENERATED_PILLARS: frozenset[str] = frozenset({"robots_crawl", "ai_discoverability"})

#: The Checker's severities, worst first. Only two exist today; an unknown one
#: sorts last rather than crashing, because a new severity upstream must not take
#: the panel down.
SEVERITY_ORDER: dict[str, int] = {"error": 0, "warn": 1}


@dataclass(frozen=True, slots=True)
class AuditFinding:
    """One recommendation from the Checker, carried rather than interpreted.

    `text` is the Checker's prose, verbatim. It is never parsed for meaning:
    `pillar` is a display label rather than a rubric key -- it takes values like
    "Cloudflare" that are not pillars at all -- so matching on it would silently
    mis-file findings.
    """

    severity: str
    pillar: str
    text: str

    @property
    def rank(self) -> int:
        return SEVERITY_ORDER.get(self.severity, len(SEVERITY_ORDER))

    @property
    def is_error(self) -> bool:
        return self.severity == "error"


@dataclass(slots=True)
class AuditView:
    """What one stored audit says, in this tool's terms.

    Deliberately holds no `ComponentStatus`. The join is by key, and the caller
    already has the statuses -- returning a merged object would give two sources
    one voice, which is the thing this module exists to prevent.
    """

    domain: str = ""
    audited_at: datetime | None = None
    overall_score: int | None = None
    grade: str = ""
    rubric_version: int | None = None
    #: component key -> what the Checker saw at its path.
    surfaces: dict[str, bool] = field(default_factory=dict)
    findings: list[AuditFinding] = field(default_factory=list)
    pillar_scores: dict[str, object] = field(default_factory=dict)

    @property
    def actionable(self) -> list[AuditFinding]:
        """Findings we can answer with a file we generate."""
        return [f for f in self.findings if f.pillar in _GENERATED_LABELS]

    @property
    def for_developer(self) -> list[AuditFinding]:
        """The rest. Not dropped -- handed over with the Checker's own words."""
        return [f for f in self.findings if f.pillar not in _GENERATED_LABELS]

    def by_pillar(self) -> dict[str, list[AuditFinding]]:
        grouped: dict[str, list[AuditFinding]] = {}
        for finding in sorted(self.findings, key=lambda f: (f.rank, f.pillar)):
            grouped.setdefault(finding.pillar, []).append(finding)
        return grouped


#: Display labels for the two pillars we can generate for. The Checker labels a
#: recommendation with the pillar's *display* name, so this is what matches.
_GENERATED_LABELS: frozenset[str] = frozenset({"Robots & Crawl", "AI Discoverability"})


def path_disagreements() -> dict[str, tuple[str, str]]:
    """The path conflicts that are still real, checked against the registry.

    Recomputed rather than trusted: if someone aligns `components.py` with the
    Checker, this returns nothing and the test that reports the conflict stops
    reporting a conflict that no longer exists.
    """
    live: dict[str, tuple[str, str]] = {}
    for key, (theirs, ours) in PATH_DISAGREEMENTS.items():
        component = by_key(key)
        if component is not None and component.path != theirs:
            live[key] = (theirs, component.path or ours)
    return live


def _surfaces(payload: dict) -> dict[str, bool]:
    """Component key -> whether the Checker found the file.

    Any of a component's mapped paths being found is enough. `/llms.txt` and
    `/llm.txt` are the same file under two names, and reporting "missing"
    because we looked under the name the site did not choose would be a finding
    about naming, not about the site.
    """
    llm = payload.get("llm_result") or {}
    raw = llm.get("raw_data") or llm
    probed: dict[str, object] = {}
    for group in ("llm_txt", "wellknown"):
        section = raw.get(group)
        if isinstance(section, dict):
            probed.update(section)

    found: dict[str, bool] = {}
    for path, key in AUDIT_PATHS.items():
        entry = probed.get(path)
        if not isinstance(entry, dict):
            continue
        found[key] = bool(entry.get("found")) or found.get(key, False)
    return found


def link_audit(payload: dict, *, domain: str = "") -> AuditView:
    """Read a stored Checker export into this tool's terms.

    Tolerant by construction. The export is a dict literal inside a Streamlit UI
    module rather than a versioned contract, so every field is optional and a
    shape change degrades the join instead of losing the audit. A malformed
    `recommendations` entry is skipped, not raised on -- the score and the
    surfaces are still worth having.
    """
    findings: list[AuditFinding] = []
    for item in payload.get("recommendations") or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        findings.append(
            AuditFinding(
                severity=str(item.get("severity") or "").lower(),
                pillar=str(item.get("pillar") or "").strip(),
                text=text,
            )
        )

    return AuditView(
        domain=domain or str(payload.get("domain") or ""),
        overall_score=_int_or_none(payload.get("overall_score")),
        grade=str(payload.get("overall_grade") or ""),
        rubric_version=_int_or_none(payload.get("rubric_version")),
        surfaces=_surfaces(payload),
        findings=sorted(findings, key=lambda f: (f.rank, f.pillar)),
        pillar_scores=payload.get("pillar_scores") or {},
    )


def _int_or_none(value: object) -> int | None:
    """`None` for anything unreadable, never 0.

    A missing score and a score of zero are different findings, and the whole
    tool turns on keeping them apart.
    """
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
