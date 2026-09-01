"""One place that decides how a state reads, and what colour it wears.

Before this module, `ComponentState` answered two different questions with one
word, and the templates each decided the colour inline. The result was measured
on a live client and it was wrong in a way that matters:

    /llms.txt      pill "ready"  (lime, positive)   detail "404"
    /llms-full.txt pill "ready"  (lime, positive)   detail "404"

Both files were absent from the client's site. The readiness score counted both
as hard failures. The tab and the score were describing the same two files and
disagreeing, because `ready` does not mean "the site has this" -- it means "we
generated a copy". Nothing on the card said the site returned 404.

**So the two questions are separated here.**

* `headline` answers *what does the client's site do* -- a fact from the probe.
  Three values, and they are the words the client-facing report already uses, so
  staff and client stop speaking differently about the same row.
* `holding` answers *what have we produced for them* -- a fact about our bundle.
  Plain prose under the chip, never a chip of its own, because it is the remedy
  and not the finding.

The old vocabulary (`live` / `ready` / `template` / `missing` / `not applicable`)
survives as the internal model. `derive()` is correct and well tested; it was
only ever its presentation that lied.

**Colour means one thing each, now.** Emerald is published. Red is not
published. Grey is not applicable. Lime is reserved for genuinely in-progress
things -- a run mid-flight, a stale snapshot -- rather than carrying, as it did,
"we prepared this", "the site returns 404", "we do not know" and "this is fine
but low priority" simultaneously.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.core.components import ComponentState

__all__ = [
    "RUN_LOOK",
    "SURFACE_LOOK",
    "Look",
    "Tone",
    "look_for",
    "run_look",
    "surface_look",
]


class Tone(StrEnum):
    """What a chip is allowed to mean. One meaning each.

    Mapped to the existing `.pill` classes rather than new ones, so the CSS
    keeps its documented contrast pairings -- but each is now pointed at a
    single idea.
    """

    #: Emerald. The client's site does this.
    GOOD = "ok"
    #: Red. It does not, and someone has to act.
    BAD = "bad"
    #: Teal outline. In flight, or not yet known.
    BUSY = "run"
    #: Plain grey. Not expected here; no action implied.
    QUIET = ""


@dataclass(frozen=True, slots=True)
class Look:
    """How one component reads on screen.

    `headline` and `tone` are the site's state. `holding` is ours, and it is
    deliberately a sentence rather than a second chip: two chips side by side
    invite the reader to weigh them against each other, when one is the finding
    and the other is only what we can do about it.
    """

    headline: str
    tone: Tone
    holding: str = ""

    @property
    def css(self) -> str:
        return f"pill {self.tone.value}".strip()


#: What the client's site does. Three values, matching `client_report.Standing`.
PUBLISHED = "Published"
NOT_PUBLISHED = "Not published"
NOT_APPLICABLE = "Not applicable"

#: What we hold, in the order `derive()` would have reached it.
_HOLDING: dict[ComponentState, str] = {
    ComponentState.LIVE: "",
    ComponentState.READY: "We have generated this file. It needs uploading.",
    ComponentState.TEMPLATE: (
        "A starting point only. The service has to exist before this can be published."
    ),
    ComponentState.MISSING: "Not something this tool can generate; it needs building.",
    ComponentState.NOT_APPLICABLE: "",
}


def look_for(status) -> Look:
    """How one `ComponentStatus` should read.

    The mapping is deliberately blunt: anything that is not confirmed on the
    client's site reads as **not published**, whatever we happen to hold for
    them. A file sitting in our bundle is not a file on their server, and the
    whole defect this module exists to fix was letting the second look like the
    first.
    """
    state = status.state

    if state is ComponentState.NOT_APPLICABLE:
        return Look(NOT_APPLICABLE, Tone.QUIET)
    if state is ComponentState.LIVE:
        return Look(PUBLISHED, Tone.GOOD)
    return Look(NOT_PUBLISHED, Tone.BAD, _HOLDING.get(state, ""))


#: The probe's own four-state vocabulary, for the overview's surface table.
#:
#: `absent` was lime here -- the same chip as `ready` -- so on one screen lime
#: meant both "we prepared this for you" and "the site publishes nothing". It is
#: red now, because a 404 is the finding the table exists to report.
#:
#: `unreachable` stays distinct and busy rather than bad: a network failure is a
#: fact about us, not about the client's site, and reporting it as an absence is
#: the error `agents_probe` is emphatic about.
SURFACE_LOOK: dict[str, Look] = {
    "present": Look(PUBLISHED, Tone.GOOD),
    "absent": Look(NOT_PUBLISHED, Tone.BAD),
    "soft_404": Look("Not published — answered with a web page", Tone.BAD),
    "wrong_type": Look("Published, but served as the wrong type", Tone.BAD),
    "unreachable": Look("Could not be checked", Tone.BUSY),
}


def surface_look(surface) -> Look:
    value = getattr(getattr(surface, "state", None), "value", "") or ""
    return SURFACE_LOOK.get(value, Look(value.replace("_", " ") or "unknown", Tone.BUSY))


#: What a crawl run is doing, in words rather than in the enum's own spelling.
#:
#: `run.html` and `index.html` each derived this inline, from the raw value with
#: its underscores swapped for spaces -- so a run sat at "awaiting review", which
#: names the state of the machine rather than telling the operator that *they*
#: are the thing it is waiting for.
#:
#: Lime (`BUSY`) is right for every non-terminal state here *except* `pending`,
#: which is the one that is not in progress at all: the row exists and nothing is
#: working on it. It takes `QUIET`, the same tone as `cancelled`, so a run nobody
#: started does not sit in the client list wearing the colour that means "working".
RUN_LOOK: dict[str, Look] = {
    # Not "Queued". A run reaches `pending` the moment its row is written, which
    # is *before* anything is deferred -- the job is only enqueued when the brief
    # gate is passed. Calling that "Waiting for a worker" sent three separate
    # investigations at the worker, which was idle and blameless every time:
    # redspot.com.au, rentalcover.com and westpac.com.au all sat here for days
    # with an empty queue. The status names the run's own state; the hint says
    # plainly that nothing is waiting to pick it up, because nothing is.
    "pending": Look("Not started", Tone.QUIET, "Nothing has been queued for this run yet."),
    "preflight": Look(
        "Sizing the site", Tone.BUSY, "Reading the sitemap before anything is spent."
    ),
    "awaiting_review": Look(
        "Waiting for you", Tone.BUSY, "Nothing is spent until the crawl plan is approved."
    ),
    "crawling": Look("Crawling", Tone.BUSY),
    "triaging": Look("Choosing pages", Tone.BUSY),
    "summarising": Look("Summarising", Tone.BUSY),
    "assembling": Look("Assembling files", Tone.BUSY),
    "complete": Look("Finished", Tone.GOOD),
    "failed": Look("Failed", Tone.BAD),
    "cancelled": Look("Stopped", Tone.QUIET, "Stopped before it finished."),
}


def run_look(status) -> Look:
    """How a run's status reads. Accepts the enum or the stored string."""
    value = getattr(status, "value", status) or ""
    return RUN_LOOK.get(str(value), Look(str(value).replace("_", " ") or "unknown", Tone.BUSY))
