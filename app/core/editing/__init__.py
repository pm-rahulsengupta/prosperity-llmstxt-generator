"""Editing a generated artefact, by hand or by asking.

One registry saying what each artefact's edits actually touch, and one gate
saying whether the result may be kept. See `registry` for why nine artefacts
need fewer than nine editors, and `gate` for why there is one gate rather than
the two that disagreed.

Nothing here talks to the database or to a model. `apply_operations` and
`apply_refinements` remain where they are and are still the only things that
mutate a source -- they are correct, well tested, and carry the safety reasoning
in their own docstrings. This package is the layer that was missing above them.
"""

from __future__ import annotations

from app.core.editing.gate import ABSOLUTE, Verdict, check
from app.core.editing.registry import (
    EDITORS,
    NOT_EDITABLE,
    ArtifactEditor,
    Scope,
    Source,
    editable,
    editor_for,
    source_of,
)

__all__ = [
    "ABSOLUTE",
    "EDITORS",
    "NOT_EDITABLE",
    "ArtifactEditor",
    "Scope",
    "Source",
    "Verdict",
    "check",
    "editable",
    "editor_for",
    "source_of",
]
