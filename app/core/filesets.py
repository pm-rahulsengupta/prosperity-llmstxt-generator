"""How a directory artefact reads before anybody clicks it.

Two of the artefacts in the registry are directories rather than files: `md/`,
one markdown file per crawled page, which on a mid-size site is four hundred of
them; and `okf/`, a cross-linked knowledge bundle with an entry point. Every
screen in this tool was built on the assumption that an artefact is one text
body with a preview and a download link, and neither half of that survives
contact with four hundred files.

**A set is summarised by its shape, never sampled.** Rendering the first
`page.md` under a heading that names the whole artefact is the category error
the rest of this codebase spends its comments avoiding -- one file shown where
an artefact was promised, which is how `ready` came to mean two things and how a
homepage came to stand in for a site. So the row states what is true of the set:
how many files, how large, and what the paths look like. The files themselves go
behind a disclosure, because a reader who wants one file is asking a different
question from a reader deciding whether to hand the set over.

**The inventory is not truncated.** The one question a file list has to answer
is "is this particular page in there", and a list that stops at twelve cannot
answer it. It scrolls inside a fixed height instead. `LIST_CAP` is a safety
valve against a runaway crawl rather than an editorial choice, and the template
says so when it fires.

**The grouped shape appears only where it earns its place.** Twenty files are
legible as a list; four hundred are not, and their useful summary is the
top-level paths they cover. One threshold decides which reading a set gets, so
`md/` and `okf/` end up looking unlike each other because they *are* unlike each
other -- rather than because somebody wrote two templates and has to keep them
in step.

**Where a set goes is read off the artefact, never off its name.** `md/`
scatters into the web root beside the HTML it mirrors, so its files answer at
`/about.md`; `okf/` drops in whole and answers under `/okf/`. Both artefact
names end in a slash and the difference is invisible in them -- but `Artifact`
carries the real root in `path`, and its `files` keys are relative to exactly
that. So `serve_note` reads `path`, and the registry's `Component.path`, which
is empty for both of these, is left out of it. Guessing here would put a
client's files in the wrong directory.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["LIST_CAP", "FileSet", "Segment", "file_sets_for", "size_label"]

#: Past this many files, a flat list stops being a picture of the set and the
#: grouped top-level paths become the summary worth reading first. Twenty-five
#: is where `okf/` (tens of files, one per concept) sits below and `md/` (one
#: per crawled page) sits above on every site measured so far.
SHAPE_FROM = 25

#: How many top-level paths the shape table names before it groups the tail. A
#: summary may summarise; it may not pretend the tail is not there, so the
#: remainder is rendered as its own row rather than dropped.
SHAPE_ROWS = 6

#: The safety valve. Not an editorial cap -- see the module docstring.
LIST_CAP = 500


def size_label(chars_or_bytes: int) -> str:
    """Rounded, and never "0 KB" for a file that exists.

    Bytes rather than characters, unlike `client_report._size`, which measures
    `len(body)` and calls the result bytes. For ASCII they agree; for a page
    title carrying an accent they do not, and the number here is the size of the
    thing that will actually be uploaded.
    """
    if chars_or_bytes < 1024:
        return f"{chars_or_bytes} bytes"
    if chars_or_bytes < 1024 * 1024:
        return f"{chars_or_bytes / 1024:.0f} KB"
    return f"{chars_or_bytes / (1024 * 1024):.1f} MB"


@dataclass(frozen=True, slots=True)
class Segment:
    """One top-level path in the set, and how many files sit under it."""

    label: str
    count: int
    #: Whether `label` is a path. The tail row -- "6 other paths" -- is prose,
    #: and setting it in the monospace face the real paths wear would invite a
    #: reader to look for a directory by that name.
    is_path: bool = True


@dataclass(frozen=True, slots=True)
class Entry:
    """One file in the set, as the inventory lists it."""

    path: str
    size: str


@dataclass(frozen=True, slots=True)
class FileSet:
    """A directory artefact, reduced to what a row has to say about it."""

    name: str
    count: int
    total_bytes: int
    listing: tuple[Entry, ...]
    #: The file a reader should open first, where the set declares one.
    #:
    #: `index.md` alone does not settle it. `md_path_for` maps `/` to `index.md`
    #: and every `/blog/` to `blog/index.md`, so a mirror of any site has one --
    #: and calling the mirrored homepage "the way in" to four hundred files tells
    #: a reader something that is technically present and practically useless.
    #: The set has to be small enough to be read as a document first; see the
    #: threshold note on `shape`.
    entry: str = ""
    shape: tuple[Segment, ...] = ()
    withheld: int = 0
    serve_note: str = ""

    @property
    def size_label(self) -> str:
        return size_label(self.total_bytes)

    @property
    def zip_name(self) -> str:
        """`md/` downloads as `md.zip`. The button says so before it is clicked.

        A link labelled with the artefact name that delivers a 1.2 MB archive is
        a small surprise, and this tool gets used with a client on the call.
        """
        return f"{self.name.rstrip('/')}.zip"


def _shape(paths: list[str]) -> tuple[Segment, ...]:
    """Top-level paths by count, biggest first, with the tail named not dropped."""
    counts: dict[str, int] = {}
    for path in paths:
        head, _, rest = path.partition("/")
        # A file at the root of the set is its own row rather than being filed
        # under a directory that does not exist.
        counts[f"{head}/" if rest else head] = counts.get(f"{head}/" if rest else head, 0) + 1

    ranked = sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
    if len(ranked) <= SHAPE_ROWS:
        return tuple(Segment(label, count) for label, count in ranked)

    head = ranked[:SHAPE_ROWS]
    tail = ranked[SHAPE_ROWS:]
    rows = [Segment(label, count) for label, count in head]
    others = sum(count for _, count in tail)
    rows.append(Segment(f"{len(tail)} other paths", others, is_path=False))
    return tuple(rows)


def _serve_note(path: str) -> str:
    """Where to put it, from the artefact's own root.

    `Artifact.files` is keyed relative to `Artifact.path`, so the two together
    are a complete instruction and neither is inferred. The root case is worded
    rather than printed: "unpack it at /" is a sentence a reader has to decode,
    and this one is read off a screen with a client on the phone.
    """
    if path in ("", "/"):
        return "Unpack it into the web root, keeping the paths shown below."
    return f"Unpack it at {path}, keeping the paths shown below."


def file_sets_for(view) -> dict[str, FileSet]:
    """Every directory artefact in the bundle, keyed by artefact name.

    Keyed by `Artifact.name` rather than by component key, because the template
    reaches this through `status.component.artifact` -- which is set on a LIVE
    component where `status.artifact_name` is not. `client_report._files` learned
    that the hard way and left a comment about it; this follows the same rule so
    a published directory still shows its shape.

    Pure and cheap. The bodies are already in the bundle, so this costs a GET
    nothing beyond the arithmetic, which is why it can sit in the shared page
    context beside `reports_for` rather than behind a cache.
    """
    if view is None:
        return {}

    sets: dict[str, FileSet] = {}

    for artifact in getattr(view.bundle, "artifacts", ()):
        # `getattr`, because `Artifact.files` is being added in parallel with
        # this. An artefact without it is a single file and belongs to the
        # existing rendering, which is also what an empty mapping means.
        files = getattr(artifact, "files", None) or {}
        if not files:
            continue

        ordered = sorted(files.items())
        total = sum(len(body.encode("utf-8")) for _, body in ordered)

        # One threshold, every consequence. A set past `SHAPE_FROM` is read as a
        # mirror -- summarised by the paths it covers, listed alphabetically
        # because that is the order its paths are guessed in, and given no front
        # door even when a file called `index.md` happens to be in it. Below the
        # threshold it is read as a document: listed in full, entry point named
        # and hoisted to the top of that list. Three separate rules here would
        # let `md/` and `okf/` drift into looking alike for reasons nobody could
        # state afterwards.
        is_mirror = len(ordered) >= SHAPE_FROM
        entry = "" if is_mirror or "index.md" not in files else "index.md"
        if entry:
            # Alphabetical order files `index.md` behind twenty concepts, so the
            # head of the block names a way in and the list underneath sends the
            # reader looking for it.
            ordered.sort(key=lambda pair: (pair[0] != entry, pair[0]))
        shown = ordered[:LIST_CAP]

        sets[artifact.name] = FileSet(
            name=artifact.name,
            count=len(ordered),
            total_bytes=total,
            listing=tuple(
                Entry(path, size_label(len(body.encode("utf-8")))) for path, body in shown
            ),
            entry=entry,
            shape=_shape([path for path, _ in ordered]) if is_mirror else (),
            withheld=len(ordered) - len(shown),
            serve_note=_serve_note(getattr(artifact, "path", "")),
        )

    return sets
