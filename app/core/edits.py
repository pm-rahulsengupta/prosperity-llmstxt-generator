"""Applying chat operations to the stored model.

This is the safety boundary between a language model and a client deliverable, so
it is deliberately suspicious of its input:

* **A URL that is not already in the run is rejected.** Not repaired, not
  fuzzy-matched, not created. The same rule `app.llm.prompts.triage.parse` applies
  to triage assignments — a model that mangles a URL must not be able to invent a
  page or silently retarget an edit at the wrong one.
* **An empty description is rejected.** Every link line needs one; the spec
  validator would flag it, and it is easier to refuse the operation than to explain
  the resulting warning.
* **Nothing is applied partially.** The caller runs these inside a transaction and
  rolls back if the rendered result gains a new error, so a turn either lands whole
  or not at all.

Rejections are returned, not raised. A turn that did four sensible things and one
impossible one should do the four and say so.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.llm.prompts.chat import Operation


@dataclass(slots=True)
class EditTarget:
    """The mutable state a turn is allowed to touch.

    Deliberately not the ORM rows: this keeps the operation logic pure and
    testable, and the caller maps the result back onto `Page` / `Run`.
    """

    site_name: str = ""
    site_summary: str = ""
    notes: str = ""
    # url -> {"title", "description", "section", "is_optional", "included"}
    pages: dict[str, dict] = field(default_factory=dict)
    section_order: list[str] = field(default_factory=list)
    renames: dict[str, str] = field(default_factory=dict)

    @property
    def section_names(self) -> list[str]:
        seen: list[str] = []
        for page in self.pages.values():
            name = page.get("section") or ""
            if name and name not in seen:
                seen.append(name)
        return seen


@dataclass(slots=True)
class EditReport:
    applied: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.applied)


def apply_operations(target: EditTarget, operations: list[Operation]) -> EditReport:
    """Mutate `target` in place. Returns what was done and what was refused."""
    report = EditReport()

    for operation in operations:
        problem = _apply_one(target, operation)
        if problem:
            report.rejected.append(f"{operation.describe()} — {problem}")
        else:
            report.applied.append(operation.describe())

    return report


def _apply_one(target: EditTarget, op: Operation) -> str:
    """Apply one operation. Returns "" on success, or why it was refused."""
    match op.op:
        case "set_site_name":
            if not op.text:
                return "no name given"
            target.site_name = op.text

        case "set_site_summary":
            if not op.text:
                return "no summary given"
            target.site_summary = op.text

        case "set_notes":
            # The spec allows any markdown except headings between the blockquote
            # and the first H2, so a heading here would make the file invalid.
            if any(line.lstrip().startswith("#") for line in op.text.splitlines()):
                return "notes cannot contain headings"
            target.notes = op.text

        case "rename_section":
            if not op.section or not op.text:
                return "needs both the old and new name"
            if op.section not in target.section_names:
                return f"no section named {op.section!r}"
            for page in target.pages.values():
                if page.get("section") == op.section:
                    page["section"] = op.text
            target.renames[op.section] = op.text
            target.section_order = [
                op.text if name == op.section else name for name in target.section_order
            ]

        case "move_page":
            if (page := target.pages.get(op.url)) is None:
                return _unknown_url(op.url)
            if not op.section:
                return "no target section"
            page["section"] = op.section
            page["is_optional"] = False

        case "set_page_copy":
            if (page := target.pages.get(op.url)) is None:
                return _unknown_url(op.url)
            if not op.title and not op.description:
                return "nothing to set"
            if op.title:
                page["title"] = op.title
            if op.description:
                page["description"] = op.description
            if not (page.get("description") or "").strip():
                return "a link line cannot have an empty description"

        case "set_optional":
            if (page := target.pages.get(op.url)) is None:
                return _unknown_url(op.url)
            page["is_optional"] = op.flag

        case "exclude_page":
            if (page := target.pages.get(op.url)) is None:
                return _unknown_url(op.url)
            page["included"] = False

        case "include_page":
            if (page := target.pages.get(op.url)) is None:
                return _unknown_url(op.url)
            page["included"] = True

        case "reorder_sections":
            known = target.section_names
            unknown = [name for name in op.sections if name not in known]
            if unknown:
                return f"unknown section(s): {', '.join(unknown)}"
            # Anything the model left out keeps its relative place at the end,
            # rather than being dropped from the file.
            target.section_order = op.sections + [n for n in known if n not in op.sections]

        case _:
            return "unsupported operation"

    return ""


def _unknown_url(url: str) -> str:
    shown = url if len(url) <= 70 else url[:67] + "..."
    return f"{shown or '(blank)'} is not a page in this run"
