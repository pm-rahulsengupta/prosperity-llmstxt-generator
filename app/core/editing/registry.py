"""Which artefacts can be edited, and what an edit to each actually touches.

Nine artefacts are generated and two accept feedback, through two complete and
separate implementations: different tables, different gates, different caps,
different scopes, different templates, different reset semantics. Adding a third
artefact meant a third copy, and the second had already diverged from the first
in ways neither could see.

This is the table that makes it one.

## The idea that makes nine tractable

**Almost every artefact is a projection of a small source model.** `md/` is not
419 independent files -- it is `render_md_pages` over the same `Page` rows that
`llms.txt` is built from, and `okf/` is those rows again through the sections
`llms.txt` already computed. `ai-info.html` is `SiteBrief.facts`. `robots.txt` is
one field of the brief.

So "shorten every description in the OKF bundle" is not a batch edit across 426
files. It is a bulk `set_page_copy` against `Page` rows, followed by the
re-render that would have happened anyway. Both prompt modules already argue for
this and neither could act on it beyond its own artefact:

    "a turn does not return a file. It returns *operations* against the same
    model the edit form already writes to ... Edits survive a later re-render
    because they are not layered on top of the data -- they are the data."
        -- app/llm/prompts/chat.py

An editor therefore names a **source**, not a file. Two artefacts sharing a
source share an editor, and the operator is told which one they are really
changing -- because editing `okf/index.md` and finding `llms.txt` different
afterwards would be alarming if it were not stated.

## Why some artefacts have no editor

`_headers` and `ai-catalog.json` are pure functions of *what the bundle
produced*. There is no prose in either and no judgement to revise: a Link header
exists because a file exists, and `build_catalog` refuses to emit at all unless
something was verified. An operator who wants a different `_headers` wants a
different bundle, and the way to get one is to change what is in it.

They are listed here as `NOT_EDITABLE` rather than left out, for the reason
`components.py` gives about templates: an artefact absent from a registry looks
like an oversight, and the next person adds it without knowing why it was not
there.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

__all__ = [
    "EDITORS",
    "NOT_EDITABLE",
    "ArtifactEditor",
    "Scope",
    "Source",
    "editable",
    "editor_for",
    "source_of",
]


class Scope(StrEnum):
    """What an edit is attached to, which decides what it survives.

    The distinction is not bookkeeping. A `RUN` edit belongs to one crawl and
    dies with it; a `DOMAIN` edit is replayed onto every future generation, which
    is what lets an `agents.md` refinement survive a re-probe. Moving `agents.md`
    to run scope would silently take that away, and it is the one property its
    storage was designed for.
    """

    RUN = "run"
    DOMAIN = "domain"


class Source(StrEnum):
    """The model an operation actually mutates."""

    #: `Page` rows plus their section assignments. The largest source by far:
    #: four artefacts are rendered from it.
    PAGES = "pages"
    #: The `AgentsDoc`, as a replayable list of `RefineOp`.
    AGENTS_DOC = "agents_doc"
    #: `SiteBrief` -- answers a person gave, not anything a probe found.
    BRIEF = "brief"


@dataclass(frozen=True, slots=True)
class ArtifactEditor:
    """One editable artefact, and what editing it means.

    `artifact` is what the operator picks. `source` is what changes. Where those
    differ the artefact is a **projection**, and `via` says so in words the UI can
    show -- an operator who asks to reword an OKF page and sees `llms.txt` change
    too should have been told, once, rather than discovering it.
    """

    artifact: str
    source: Source
    scope: Scope
    #: The rule set that judges the result, by the name `evidence.JUDGED_BY` uses.
    judged_by: str
    #: Empty where the artefact *is* its source. Otherwise, why they move together.
    via: str = ""
    #: Operations this artefact's own surface should offer. Empty means the whole
    #: vocabulary of the source applies.
    only: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_projection(self) -> bool:
        return bool(self.via)


#: Keyed by artefact name, matching `components.Component.artifact` exactly, so a
#: rename in the registry breaks this rather than silently orphaning an editor.
EDITORS: dict[str, ArtifactEditor] = {
    "llms.txt": ArtifactEditor(
        artifact="llms.txt",
        source=Source.PAGES,
        scope=Scope.RUN,
        judged_by="index",
    ),
    "llms-full.txt": ArtifactEditor(
        artifact="llms-full.txt",
        source=Source.PAGES,
        scope=Scope.RUN,
        judged_by="full",
        via=(
            "The full file is the same pages with their bodies attached, so it "
            "changes when the pages do. Which pages it reaches is a budget, not "
            "an edit."
        ),
    ),
    "md/": ArtifactEditor(
        artifact="md/",
        source=Source.PAGES,
        scope=Scope.RUN,
        judged_by="markdown",
        via=(
            "Each file here is one page's own markdown. Changing a page's title "
            "or description changes its twin and the llms.txt line that names it, "
            "because all three are rendered from the same row."
        ),
    ),
    "okf/": ArtifactEditor(
        artifact="okf/",
        source=Source.PAGES,
        scope=Scope.RUN,
        judged_by="okf",
        via=(
            "The bundle is the sections llms.txt already computed, one file per "
            "concept. Renaming a section renames its concept file and every link "
            "into it."
        ),
    ),
    "agents.md": ArtifactEditor(
        artifact="agents.md",
        source=Source.AGENTS_DOC,
        scope=Scope.DOMAIN,
        judged_by="agents",
    ),
    "ai-info.html": ArtifactEditor(
        artifact="ai-info.html",
        source=Source.BRIEF,
        scope=Scope.DOMAIN,
        judged_by="info",
        via=(
            "Every claim on this page comes from a fact in the brief, and a fact "
            "carries its source or does not exist. So the page is edited by "
            "editing the brief -- which is also why it is thin when the brief is."
        ),
    ),
    "robots.txt": ArtifactEditor(
        artifact="robots.txt",
        source=Source.BRIEF,
        scope=Scope.DOMAIN,
        judged_by="crawl",
        via=(
            "This block is the stated AI-bot policy, written out. Changing it "
            "means changing the policy, which is a decision recorded in the brief "
            "rather than prose to revise."
        ),
        only=("set_bot_policy",),
    ),
}

#: Generated, judged, and deliberately not editable. Each is a pure function of
#: what the bundle produced, so there is no prose to revise and no judgement to
#: overrule -- an operator who wants a different one wants a different bundle.
NOT_EDITABLE: dict[str, str] = {
    "_headers": (
        "Every line here exists because a file exists. Editing it directly is how "
        "a Link header comes to point at something that was never generated, "
        "which is the one thing HDR-001 is for."
    ),
    "ai-catalog.json": (
        "Assembled from endpoints a probe confirmed answer. `build_catalog` "
        "refuses to emit one at all where nothing was verified, so there is "
        "nothing here a person could correct that would still be true."
    ),
}


def editor_for(artifact: str) -> ArtifactEditor | None:
    """The editor for one artefact, or `None` where there is none.

    `None` and "not in the registry at all" are deliberately the same answer to
    callers, and different to `editable()`: a caller deciding whether to render
    an edit button wants one boolean, and a caller explaining why not wants the
    sentence from `NOT_EDITABLE`.
    """
    return EDITORS.get(artifact)


def editable(artifact: str) -> bool:
    return artifact in EDITORS


def source_of(artifact: str) -> Source | None:
    editor = EDITORS.get(artifact)
    return editor.source if editor else None
