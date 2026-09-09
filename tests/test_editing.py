"""The editing registry and the one gate.

Two properties carry most of the weight here. Every generated artefact must be
either editable or explicitly not, for the reason `JUDGED_BY` has the same
property: an artefact missing from a registry reads as an oversight, and the next
person adds it without knowing why it was absent. And the gate must be the
stricter of the two it replaces, because the looser one demonstrably let an edit
worsen a failure it already had.
"""

from __future__ import annotations

import pytest

from app.core.components import COMPONENTS
from app.core.editing import (
    ABSOLUTE,
    EDITORS,
    NOT_EDITABLE,
    Scope,
    Source,
    check,
    editable,
    editor_for,
)
from app.core.evidence import JUDGED_BY
from app.core.rules.registry import Category, Outcome, Rule, Severity

# -- the registry covers what the tool produces ---------------------------------


def test_every_generated_artifact_is_editable_or_says_why_not():
    """The gap this registry closes, asserted the way `JUDGED_BY` asserts its own.

    Seven of nine artefacts had no edit path at all, each with a rule set beside
    it reporting findings nobody could act on without re-running the pipeline.
    """
    artifacts = {c.artifact for c in COMPONENTS if c.artifact}
    covered = set(EDITORS) | set(NOT_EDITABLE)

    assert artifacts - covered == set(), "an artifact is generated and unreachable"


def test_nothing_is_both_editable_and_refused():
    assert set(EDITORS) & set(NOT_EDITABLE) == set()


def test_every_editor_names_a_rule_set_that_exists():
    """An editor whose gate names nothing would keep every edit."""
    known = set(JUDGED_BY.values())

    for editor in EDITORS.values():
        assert editor.judged_by in known, f"{editor.artifact} is judged by nothing real"


def test_every_editor_names_an_artifact_the_registry_produces():
    """Keyed on `Component.artifact`, so a rename breaks here rather than quietly
    orphaning an editor."""
    artifacts = {c.artifact for c in COMPONENTS if c.artifact}

    assert set(EDITORS) <= artifacts
    assert set(NOT_EDITABLE) <= artifacts


# -- the projection model -------------------------------------------------------


def test_the_four_page_artifacts_share_one_source():
    """The finding that makes editing 845 files tractable.

    `md/` and `okf/` are re-renders of the rows `llms.txt` is built from, so a
    bulk description rewrite is one operation against pages, not a batch edit
    across 426 files.
    """
    from_pages = {a for a, e in EDITORS.items() if e.source is Source.PAGES}

    assert from_pages == {"llms.txt", "llms-full.txt", "md/", "okf/"}


def test_a_projection_says_what_it_really_changes():
    """An operator who rewords an OKF concept and finds llms.txt different
    afterwards should have been told once, rather than discovering it."""
    for editor in EDITORS.values():
        if editor.artifact == "llms.txt":
            continue
        if editor.source is Source.PAGES:
            assert editor.is_projection, f"{editor.artifact} changes pages and does not say so"
            assert len(editor.via) > 40, f"{editor.artifact} explains itself too thinly"


def test_agents_md_stays_domain_scoped():
    """Its edits are stored as replayable operations precisely so they survive a
    re-probe. Run scope would take that away silently."""
    assert editor_for("agents.md").scope is Scope.DOMAIN
    assert editor_for("llms.txt").scope is Scope.RUN


def test_the_two_that_cannot_be_edited_are_the_two_that_assert_nothing_new():
    """Both are pure functions of what the bundle produced. Editing `_headers`
    directly is exactly how a Link header comes to name a file nobody generated,
    which is the failure HDR-001 exists for."""
    assert set(NOT_EDITABLE) == {"_headers", "ai-catalog.json"}
    assert not editable("_headers")
    for reason in NOT_EDITABLE.values():
        assert len(reason) > 60, "a refusal an operator cannot understand is an apology"


# -- the gate -------------------------------------------------------------------


def _rule(rule_id: str, severity: Severity = Severity.ERROR) -> Rule:
    return Rule(rule_id, rule_id, Category.INDEX, severity, lambda ctx: None, "")


class _Finding:
    def __init__(self, rule_id: str, count: int = 1, outcome=Outcome.FAIL, message: str = "x"):
        self.rule_id = rule_id
        self.count = count
        self.outcome = outcome
        self.message = message


class _Report:
    def __init__(self, *findings: _Finding):
        self.failures = list(findings)

    def by_id(self, rule_id: str):
        return next((f for f in self.failures if f.rule_id == rule_id), None)

    def failed(self, rule_id: str) -> bool:
        finding = self.by_id(rule_id)
        return finding is not None and finding.outcome is Outcome.FAIL


RULES = {r.id: r for r in (_rule("IDX-001"), _rule("IDX-002"), _rule("IDX-003", Severity.WARNING))}


def test_a_clean_edit_is_kept():
    verdict = check(_Report(), _Report(), judged_by="index", rules_by_id=RULES)

    assert verdict.kept
    assert bool(verdict) is True


def test_a_rule_that_starts_failing_is_refused():
    verdict = check(_Report(), _Report(_Finding("IDX-001")), judged_by="index", rules_by_id=RULES)

    assert not verdict.kept
    assert "IDX-001 would start failing" in verdict.reasons


def test_a_failure_that_grows_is_refused():
    """The test the llms.txt gate did not have.

    It compared the *set of codes*, so one relative URL becoming forty passed --
    which is the exact shape of a bulk rewrite of every description at once.
    """
    verdict = check(
        _Report(_Finding("IDX-001", count=1)),
        _Report(_Finding("IDX-001", count=40)),
        judged_by="index",
        rules_by_id=RULES,
    )

    assert not verdict.kept
    assert "IDX-001 would go from 1 to 40" in verdict.reasons


def test_a_failure_that_shrinks_is_kept():
    verdict = check(
        _Report(_Finding("IDX-001", count=40)),
        _Report(_Finding("IDX-001", count=1)),
        judged_by="index",
        rules_by_id=RULES,
    )

    assert verdict.kept


def test_a_new_warning_does_not_block():
    """Blocking these teaches an operator to stop asking for legitimate changes,
    which costs more than the warning does."""
    verdict = check(_Report(), _Report(_Finding("IDX-003")), judged_by="index", rules_by_id=RULES)

    assert verdict.kept


def test_an_absolute_is_refused_even_when_it_was_already_failing():
    """Not "newly failed" -- failed. A file naming an endpoint no probe confirmed
    sends an agent somewhere we invented, and it having done so before this edit
    is not a defence."""
    before = _Report(_Finding("AGT-004", message="names an unverified URL"))
    after = _Report(_Finding("AGT-004", message="names an unverified URL"))

    verdict = check(before, after, judged_by="agents", rules_by_id={})

    assert not verdict.kept
    assert "AGT-004" in verdict.reasons[0]


def test_a_first_draft_is_judged_only_against_its_absolutes():
    """With no `before`, everything in `after` is new. Judging a first generation
    against a predecessor it has not got would refuse every one of them."""
    kept = check(None, _Report(_Finding("IDX-001")), judged_by="index", rules_by_id=RULES)
    refused = check(
        None, _Report(_Finding("AGT-004", message="m")), judged_by="agents", rules_by_id={}
    )

    assert kept.kept
    assert not refused.kept


@pytest.mark.parametrize("judged_by", sorted({e.judged_by for e in EDITORS.values()}))
def test_every_editable_artifact_has_an_absolutes_entry(judged_by: str):
    """An empty tuple is a decision; a missing key is an oversight, and reads
    identically at the call site."""
    assert judged_by in ABSOLUTE


# -- the projection model, proven rather than asserted ---------------------------


def test_one_operation_against_pages_changes_all_four_files_it_renders():
    """The claim the whole registry rests on.

    If this fails, "edit the OKF bundle" cannot mean "edit the pages", and the
    design collapses back into a batch edit across 426 files. So it is proven
    end to end against the real renderers rather than asserted in a docstring.
    """
    from datetime import date

    from app.core.edits import EditTarget, apply_operations
    from app.core.md_pages import render_md_pages
    from app.core.models import PageEntry, Section
    from app.core.okf import render_okf
    from app.core.render import render_llmstxt
    from app.llm.prompts.chat import Operation

    url = "https://x.example/vehicles/ute-hire/"
    body = (
        "Utes for transport and commercial use, with maintenance included and "
        "roadside assistance on every booking across the network."
    )

    def render(description: str) -> dict[str, str]:
        page = PageEntry(url=url, title="Ute Hire", description=description, markdown=body)
        sections = [Section(name="Vehicles", pages=[page])]
        md = render_md_pages([page])
        okf = render_okf(
            "https://x.example", "X", "A summary.", sections, generated_on=date(2026, 9, 9)
        )
        return {
            "llms.txt": render_llmstxt(
                "https://x.example", "X", "A summary.", sections, [], generated_on=date(2026, 9, 9)
            ),
            "md/": md.files["vehicles/ute-hire/index.md"],
            "okf/": okf.files["pages/ute-hire.md"],
        }

    # The operation an operator would actually ask for, through the real path.
    target = EditTarget(pages={url: {"title": "Ute Hire", "description": "Old wording."}})
    report = apply_operations(
        target, [Operation(op="set_page_copy", url=url, description="New wording entirely.")]
    )
    assert report.applied and not report.rejected, report.rejected

    before = render("Old wording.")
    after = render(target.pages[url]["description"])

    for name in ("llms.txt", "md/", "okf/"):
        assert "Old wording." in before[name], f"{name} did not carry the description to begin with"
        assert "New wording entirely." in after[name], f"{name} did not follow the page row"
        assert "Old wording." not in after[name], f"{name} kept the old wording"


# -- the operation that reported success and changed nothing ---------------------


def test_set_notes_reaches_the_file():
    """`Run.notes` existed in the schema, `edits.set_notes` validated it and
    refused headings, `main` persisted it -- and `render_llmstxt` took no `notes`
    parameter, so the operation applied cleanly, reported success to the operator,
    and changed no byte of the file.
    """
    from datetime import date

    from app.core.edits import EditTarget, apply_operations
    from app.core.models import PageEntry, Section
    from app.core.render import render_llmstxt
    from app.llm.prompts.chat import Operation

    target = EditTarget()
    note = "Redspot is the car rental company, not the photography studio of the same name."
    report = apply_operations(target, [Operation(op="set_notes", text=note)])
    assert report.applied and not report.rejected, report.rejected

    sections = [Section(name="S", pages=[PageEntry(url="https://x.example/a/", title="A")])]
    rendered = render_llmstxt(
        "https://x.example",
        "X",
        "A summary.",
        sections,
        [],
        generated_on=date(2026, 9, 9),
        notes=target.notes,
    )

    assert note in rendered


def test_the_notes_block_sits_where_the_spec_allows_prose():
    """Between the blockquote and the first H2. The spec permits any markdown
    except headings there, which is why `edits.set_notes` refuses a line starting
    with `#` -- a heading would end the block and make the file invalid."""
    from datetime import date

    from app.core.models import PageEntry, Section
    from app.core.render import render_llmstxt

    sections = [Section(name="Services", pages=[PageEntry(url="https://x.example/a/", title="A")])]
    rendered = render_llmstxt(
        "https://x.example",
        "X",
        "A summary.",
        sections,
        [],
        generated_on=date(2026, 9, 9),
        notes="A disambiguating sentence.",
    )

    quote = rendered.index("> A summary.")
    note = rendered.index("A disambiguating sentence.")
    first_h2 = rendered.index("## Services")

    assert quote < note < first_h2


def test_a_rebuild_does_not_drop_the_notes():
    """The failure mode the source tool had with section descriptions: an edit to
    one thing silently discarding another."""
    from app.core.models import GenerationResult, PageEntry, Section
    from app.core.pipeline import rebuild

    pages = [PageEntry(url=f"https://x.example/{n}/", title=n.upper()) for n in "ab"]
    result = GenerationResult(
        site_url="https://x.example",
        site_name="X",
        site_summary="A summary.",
        pattern="catalog",
        sections=[Section(name="S", pages=pages)],
        notes="Carried through.",
    )

    rebuilt = rebuild(result, excluded_urls={"https://x.example/b/"})

    assert rebuilt.notes == "Carried through."
    assert "Carried through." in rebuilt.llmstxt


# -- what an interactive model call owes ----------------------------------------


def test_every_interactive_model_call_consults_the_daily_ceiling():
    """Three routes build an `LLMClient` outside the job queue and one consulted
    the cap.

    `repo.spend_today` existed and only the refine panel called it, so a chat
    session could spend without limit while a refine session on the same client
    stopped at 120 -- and the brief wizard, which is the most expensive single
    press in the product, was uncapped from the day it was written.

    Read from the source rather than exercised, because exercising it means
    spending money with the vendor to prove we would have refused to.
    """
    import re
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text(encoding="utf-8")

    # Each handler runs from its `async def` to the next one at column zero.
    handlers = re.split(r"\n(?=@app\.|async def |def )", source)
    interactive = [h for h in handlers if "LLMClient(settings" in h]

    assert len(interactive) >= 3, "the split stopped finding the handlers; fix the test"

    uncapped = [
        re.search(r"async def (\w+)", h).group(1)
        for h in interactive
        if "spend_today" not in h and re.search(r"async def (\w+)", h)
    ]

    assert uncapped == [], f"interactive model call with no ceiling: {uncapped}"


def test_a_refused_turn_still_records_what_it_spent():
    """`record_spend` ran inside the transaction the gate rolls back, so a refused
    edit billed a gpt-4o call and reached the costs page as nothing -- which is
    the defect the comment above that call describes, reintroduced by the gate
    added after it."""
    import re
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text(encoding="utf-8")

    # Scoped to the chat handler. There is an earlier `rollback()` on an auth
    # path, and anchoring on the first occurrence tested that one instead --
    # which is the shape of mistake this whole file exists to catch.
    handlers = re.split(r"\n(?=@app\.)", source)
    chat = next(h for h in handlers if 'post("/runs/{run_id}/chat"' in h)

    after_rollback = chat.split("await session.rollback()", 1)
    assert len(after_rollback) == 2, "the chat gate no longer rolls back; re-read this test"

    assert "record_spend" in after_rollback[1], "a rolled-back turn costs money and records none"
