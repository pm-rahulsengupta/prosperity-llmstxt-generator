"""A string-replace edit that cannot fail silently.

Four silent no-ops in two sessions, each a different cause and each invisible:

* a search string whose escapes were mangled in transit, so it matched nothing;
* a search string ruff had already reformatted from single to double quotes;
* a rewrite that truncated a file tail and took a function with it;
* an edit that matched nothing, leaving an import unused, which ruff then
  removed -- deleting the only remaining evidence that the edit was attempted.

All four passed the whole test suite. That is not a discipline problem, because
the failure produces no signal to be disciplined about: `str.replace` returns a
string whether or not it replaced anything, and a test suite that asserts on
behaviour cannot see an artifact that never changed. `git diff --stat` catches
it afterwards and depends on someone remembering to look.

So the check moves before the write. Every function here raises rather than
returning a status, because a status is something a caller can forget to read --
which is the bug this module exists to remove.

Usage::

    from tools.edit import replace_once, ensure_absent, ensure_present

    replace_once("app/core/metrics.py", old, new)
    ensure_present("app/core/metrics.py", "def canonical_metric_url")
"""

from __future__ import annotations

import difflib
from pathlib import Path

__all__ = ["EditError", "ensure_absent", "ensure_present", "insert_before", "replace_once"]


class EditError(RuntimeError):
    """An edit did not do what it said. Never raised for a legitimate no-op."""


def _read(path: str | Path) -> tuple[Path, str]:
    target = Path(path)
    if not target.is_file():
        raise EditError(f"{target} does not exist")
    return target, target.read_text(encoding="utf-8")


def _near_misses(haystack: str, needle: str, limit: int = 3) -> str:
    """Why the match failed, in the form the caller can act on.

    Reporting only "not found" leaves the caller guessing between a typo, a
    reformatting, and an edit that already applied. The closest lines usually
    say which immediately -- single quotes where doubles were expected is the
    reformatting case, and it has happened twice.
    """
    first = needle.strip().splitlines()[0] if needle.strip() else needle
    close = difflib.get_close_matches(first, haystack.splitlines(), n=limit, cutoff=0.6)
    if not close:
        return "No similar line found. Is the file the one you meant?"
    lines = "\n".join(f"    {line.strip()[:100]}" for line in close)
    return f"Closest lines in the file:\n{lines}"


def replace_once(path: str | Path, old: str, new: str, *, count: int = 1) -> None:
    """Replace `old` with `new`, or raise.

    Refuses on zero matches and on more matches than `count`. The second is as
    important as the first: a search string that matches three places when one
    was meant edits two things nobody looked at.
    """
    target, text = _read(path)

    found = text.count(old)
    if found == 0:
        if new and new in text:
            raise EditError(
                f"{target}: the search string is absent but the replacement is already "
                "present. This edit looks like it has already been applied."
            )
        raise EditError(f"{target}: search string not found.\n{_near_misses(text, old)}")
    if found > count:
        raise EditError(
            f"{target}: search string matches {found} times, expected at most {count}. "
            "Narrow it, or pass count= deliberately."
        )

    updated = text.replace(old, new, count)
    if updated == text:
        raise EditError(f"{target}: replacement produced no change; old and new are identical.")

    target.write_text(updated, encoding="utf-8")


def insert_before(path: str | Path, anchor: str, addition: str) -> None:
    """Insert `addition` immediately before `anchor`, or raise."""
    replace_once(path, anchor, addition + anchor)


def ensure_present(path: str | Path, *needles: str) -> None:
    """Assert every needle is in the file after an edit.

    The complement of `replace_once`: that one proves the write happened, this
    proves the result is what was wanted. Worth running after a formatter, which
    is where the fourth no-op came from -- the edit landed and ruff then removed
    the part of it that was not yet referenced.
    """
    _, text = _read(path)
    if missing := [n for n in needles if n not in text]:
        raise EditError(f"{path}: expected but absent: {missing}")


def ensure_absent(path: str | Path, *needles: str) -> None:
    """Assert a removal actually removed something."""
    _, text = _read(path)
    if present := [n for n in needles if n in text]:
        raise EditError(f"{path}: expected to be gone but still present: {present}")
