"""Joining an LLM Access Checker audit to the components this tool can fix.

The Checker is the diagnosis; this tool is the remediation. Its rubric is far
wider than anything here can generate, and the join has to make that visible
rather than imply the tool fixes everything.

**Its rubric moves, and ours must not pretend otherwise.** Production runs v5:
sixteen pillars under Crawl (35%), Comprehend (26%), Convince (28%) and Convert
(11%), with Security scored and deliberately left out of the average. The prose
that used to sit here described v2's six flat pillars and was two releases out
of date -- which is exactly the failure this module exists to prevent, committed
in its own docstring. The rubric now lives in `PILLAR_WEIGHTS`, keyed by version,
and the pillars themselves are read from the payload so a version we have never
seen still renders.

Of v5, roughly **22% maps onto files we generate** -- Robots & Crawl, AI
Discoverability, and Machine Readability, which `md/` and `okf/` answer directly.
The rest does not. That second group is not dropped: it becomes developer-handover
work carrying the Checker's own recommendation text, attributed to it.

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
    "PILLAR_LABELS",
    "PILLAR_WEIGHTS",
    "AuditFinding",
    "AuditView",
    "PillarScore",
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
#: `machine_readability` joined this when `md/` and `okf/` were added -- v5 scores
#: markdown serving as its own pillar, and it is the one glassons.com currently
#: reports as unmeasured. `content_citability` is the v2 name for a pillar v5
#: split five ways, none of which we generate.
GENERATED_PILLARS: frozenset[str] = frozenset(
    {"robots_crawl", "ai_discoverability", "machine_readability"}
)

#: Weights per rubric version, because the Checker reweights between them and
#: says so: "Scores from different versions are different measurements wearing
#: the same unit, so the tool refuses to trend them together."
#:
#: The first version of this table hardcoded v2 and was already two rubrics out
#: of date when it shipped -- production runs **v5**, whose pillars are grouped
#: under Crawl, Comprehend, Convince and Convert with Security reported outside
#: the average. Every row would have rendered "not scored" against a live audit.
#:
#: So weights are keyed by version and everything else is read from the payload.
#: A rubric we have no weights for still renders its scores; it just does not
#: claim to know what they are worth.
PILLAR_WEIGHTS: dict[int, dict[str, int]] = {
    # Six pillars, flat. JS 15, Robots 20, Schema 25, AI Discovery 15,
    # Agent Interaction 15, Content & Citability 10.
    2: {
        "schema_entity": 25,
        "robots_crawl": 20,
        "js_rendering": 15,
        "ai_discoverability": 15,
        "ai_interactivity": 15,
        "content_citability": 10,
    },
    3: {},
    4: {},
    # Sixteen pillars under four stages. The effective weight is the stage's
    # share of the overall multiplied by the pillar's share of its stage --
    # Crawl 35 x Robots 37 = 13, and so on. Rounded, so they sum to about 100
    # rather than exactly: the Checker clamps and renormalises, and inventing
    # precision it does not claim would be the same error as the version it
    # replaced.
    5: {
        "robots_crawl": 13,
        "js_rendering": 13,
        "performance_crawlability": 4,
        "ai_discoverability": 5,
        "schema_entity": 9,
        "semantic_html": 5,
        "agent_accessibility": 5,
        "meta_discoverability": 4,
        "internal_linking": 3,
        "citability_answer_readiness": 9,
        "factual_verifiability": 7,
        "information_density": 6,
        "entity_authority": 4,
        "content_freshness": 2,
        "ai_interactivity": 7,
        "machine_readability": 4,
    },
}

#: Display names for pillars we recognise, across versions. A slug that is not
#: here is humanised from itself rather than dropped -- the panel must not lose a
#: pillar because we have not met it, which is the failure the first version of
#: this table would have had against every current audit.
PILLAR_LABELS: dict[str, str] = {
    "robots_crawl": "Robots & Crawl",
    "js_rendering": "JS Rendering",
    "performance_crawlability": "Performance & Crawlability",
    "ai_discoverability": "AI Discoverability",
    "schema_entity": "Schema & Entity",
    "semantic_html": "Semantic HTML",
    "agent_accessibility": "Agent Accessibility",
    "meta_discoverability": "Meta & Discoverability",
    "internal_linking": "Internal Linking",
    "citability_answer_readiness": "Citability & Answer-Readiness",
    "factual_verifiability": "Factual Verifiability",
    "information_density": "Information Density",
    "entity_authority": "Entity & Authority",
    "content_freshness": "Content Freshness",
    "ai_interactivity": "AI Interactivity",
    "machine_readability": "Machine Readability",
    "content_citability": "Content & Citability",
    "security": "Security",
}


@dataclass(frozen=True, slots=True)
class PillarScore:
    """One pillar of the Checker's rubric, and whether we can act on it."""

    slug: str
    label: str
    #: None where we hold no weights for this rubric version. A weight we have
    #: not got is not a weight of zero, and guessing one is how the first version
    #: of this table came to describe a rubric two releases out of date.
    weight: int | None
    score: int | None
    generated: bool

    @property
    def measured(self) -> bool:
        """False where the export carried no score for this pillar.

        Kept distinct from a score of zero, which is the same rule the nav's
        `gap: int | None` states: a pillar nobody scored must not read as a
        pillar that scored nothing.
        """
        return self.score is not None


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

    def pillars(self) -> list[PillarScore]:
        """The rubric, scored, in weight order.

        `pillar_scores` has been stored on every ingested audit and read by
        nothing, so the panel showed one overall number. That number cannot
        distinguish "strong where we can help, weak where we cannot" from its
        opposite -- and those are opposite conversations to have with a client.

        A pillar the export did not score is carried with `score=None` rather
        than dropped, because a rubric with two of six rows shown reads as a
        two-row rubric.
        """
        weights = PILLAR_WEIGHTS.get(self.rubric_version or 0, {})
        raw = dict(self.pillar_scores or {})
        # Every pillar the rubric has, not only the ones the export scored. A
        # sixteen-pillar rubric showing two rows reads as a two-pillar rubric,
        # which understates what the client is being graded on -- and the missing
        # ones render as "not scored", which is the finding.
        #
        # Union rather than either alone: the payload can carry a pillar our
        # weights table has not met, and dropping it would be the staleness this
        # module has already made once.
        for slug in weights:
            raw.setdefault(slug, None)

        out: list[PillarScore] = []
        for slug, value in raw.items():
            out.append(
                PillarScore(
                    slug=slug,
                    label=PILLAR_LABELS.get(slug) or slug.replace("_", " ").title(),
                    weight=weights.get(slug),
                    score=_int_or_none(value),
                    generated=slug in GENERATED_PILLARS,
                )
            )
        # Heaviest first where we know the weights, so the order is the order the
        # work matters in. Otherwise the payload's own order, which is the
        # Checker's, and is not ours to reinterpret.
        if weights:
            out.sort(key=lambda p: (-(p.weight or 0), p.label))
        return out

    @property
    def generated_weight(self) -> int | None:
        """Share of this rubric this tool can produce a file for.

        Stated rather than implied: the integration's whole risk is reading as
        though the tool fixes everything the Checker measures. `None` where we
        hold no weights for the version, because a share of a rubric we cannot
        weigh is a number with no meaning.
        """
        weights = PILLAR_WEIGHTS.get(self.rubric_version or 0, {})
        if not weights:
            return None
        return sum(w for slug, w in weights.items() if slug in GENERATED_PILLARS)

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
