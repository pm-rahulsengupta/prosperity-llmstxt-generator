"""The safety boundary between a language model and a client's web root.

agents.md contains instructions an agent will follow, so a wrong line is not
untidy -- it is acted on. Every test here is about what the refine layer
*refuses*.
"""

from __future__ import annotations

from app.core.agents_doc import AgentsDoc, Capability, PolicyLink
from app.core.refine import (
    MAX_FACT,
    OPERATIONS,
    AssertedFact,
    RefineOp,
    apply_refinements,
)


def doc() -> AgentsDoc:
    return AgentsDoc(
        site_url="https://x.example",
        site_name="X",
        summary="An agency.",
        agent_guidance="Canonical site: https://x.example",
        capabilities=[
            Capability(label="Search", url="https://x.example/search", evidence="answered 200")
        ],
        read_only_urls=[
            Capability(label="About", url="https://x.example/about", evidence="crawled")
        ],
        policies=[PolicyLink(label="Privacy", url="https://x.example/privacy")],
    )


def run(*ops, facts=None, author="a@b.c"):
    return apply_refinements(doc(), list(facts or []), list(ops), author=author)


# -- what it refuses ----------------------------------------------------------


def test_no_operation_can_introduce_a_url():
    """The whole point. There is no `add_capability`, by construction."""
    adding = {name for name in OPERATIONS if name.startswith(("add_", "set_"))}

    assert adding == {
        "add_fact",
        "set_summary",
        "set_guidance",
        "set_site_name",
        "set_rate_limit_note",
    }
    assert "add_capability" not in OPERATIONS
    assert "add_policy" not in OPERATIONS
    assert "add_endpoint" not in OPERATIONS


def test_prose_containing_a_url_is_refused():
    """A model asked to "mention our API docs" would otherwise write one in."""
    _doc, _facts, report = run(
        RefineOp(op="set_summary", text="An agency. See https://x.example/api for our API.")
    )

    assert report.applied == []
    assert "may not contain a URL" in report.rejected[0]
    assert "onboarding" in report.rejected[0], "it names the path that does work"


def test_an_operator_fact_containing_a_url_is_refused():
    """The boundary restated where somebody would walk around it."""
    _doc, facts, report = run(RefineOp(op="add_fact", text="Docs live at www.x.example/docs"))

    assert facts == []
    assert "an agent follows urls" in report.rejected[0].lower()


def test_a_bare_domain_is_still_a_url():
    _doc, _facts, report = run(RefineOp(op="add_fact", text="Find us at www.x.example"))

    assert report.rejected


def test_an_unknown_operation_is_refused_rather_than_guessed_at():
    _doc, _facts, report = run(RefineOp(op="rewrite_everything", text="go on"))

    assert "not an operation this file supports" in report.rejected[0]


def test_relabelling_a_capability_that_is_not_there_is_refused():
    """Not created, not fuzzy-matched."""
    _doc, _facts, report = run(
        RefineOp(op="relabel_capability", url="https://other.example/x", text="Thing")
    )

    assert "no capability with the URL" in report.rejected[0]


def test_prose_has_a_length_limit():
    _doc, _facts, report = run(RefineOp(op="set_summary", text="x" * 5_000))

    assert "past the" in report.rejected[0]


def test_a_fact_has_a_tighter_limit_than_prose():
    _doc, facts, report = run(RefineOp(op="add_fact", text="y" * (MAX_FACT + 1)))

    assert facts == []
    assert report.rejected


def test_one_refusal_does_not_lose_the_other_operations():
    """A model asking one impossible thing among several reasonable ones should
    get the reasonable ones, and be told about the other."""
    updated, _facts, report = run(
        RefineOp(op="set_summary", text="A Sydney SEO agency."),
        RefineOp(op="set_guidance", text="Read https://x.example/start first"),
        RefineOp(op="set_site_name", text="Prosperity"),
    )

    assert updated.summary == "A Sydney SEO agency."
    assert updated.site_name == "Prosperity"
    assert len(report.applied) == 2
    assert len(report.rejected) == 1


# -- what it allows -----------------------------------------------------------


def test_prose_is_rewritten():
    updated, _facts, report = run(RefineOp(op="set_summary", text="A Sydney SEO agency."))

    assert updated.summary == "A Sydney SEO agency."
    assert report.changed


def test_a_capability_can_be_relabelled_without_touching_its_url():
    updated, _facts, _report = run(
        RefineOp(op="relabel_capability", url="https://x.example/search", text="Find a service")
    )

    assert updated.capabilities[0].label == "Find a service"
    assert updated.capabilities[0].url == "https://x.example/search", "the URL is untouched"


def test_dropping_a_capability_narrows_the_file():
    """Removing something a probe found is always safe: the result claims less
    than the evidence supports."""
    updated, _facts, _report = run(RefineOp(op="drop_capability", url="https://x.example/search"))

    assert updated.capabilities == []
    assert updated.read_only_urls, "only the named one goes"


def test_a_read_only_link_can_be_dropped_too():
    updated, _facts, _report = run(RefineOp(op="drop_capability", url="https://x.example/about"))

    assert updated.read_only_urls == []


def test_a_policy_can_be_dropped():
    updated, _facts, _report = run(RefineOp(op="drop_policy", url="https://x.example/privacy"))

    assert updated.policies == []


def test_the_original_document_is_never_mutated():
    """A rejected turn must not leave the file half-edited."""
    original = doc()
    updated, _facts, _report = apply_refinements(
        original, [], [RefineOp(op="set_summary", text="Changed.")], author="a@b.c"
    )

    assert original.summary == "An agency."
    assert updated.summary == "Changed."


# -- operator-asserted facts --------------------------------------------------


def test_a_stated_fact_carries_who_said_it_and_when():
    _doc, facts, _report = run(
        RefineOp(op="add_fact", text="Returns accepted within 30 days."),
        author="rahul@prosperitymedia.com.au",
    )

    assert len(facts) == 1
    assert facts[0].noted_by == "rahul@prosperitymedia.com.au"
    assert facts[0].noted_at


def test_a_stated_fact_renders_as_unverified():
    """The distinction has to survive into the file itself, not just the UI."""
    rendered = AssertedFact(
        text="Returns accepted within 30 days.", noted_by="a@b.c", noted_at="2026-08-24"
    ).render()

    assert "not independently verified" in rendered
    assert "2026-08-24" in rendered
    assert "a@b.c" not in rendered, (
        "AGT-006 caught this: the file is fetched by anyone, forever, so a "
        "colleague's email must not be published to make a note read better"
    )


def test_facts_can_be_cleared():
    existing = [AssertedFact(text="Old.", noted_by="a@b.c", noted_at="2026-01-01")]
    _doc, facts, report = run(RefineOp(op="clear_facts"), facts=existing)

    assert facts == []
    assert report.changed


def test_every_operation_in_the_vocabulary_is_implemented():
    """A name in OPERATIONS that nothing handles reaches the model as an offer."""
    for name in OPERATIONS:
        _doc, _facts, report = run(
            RefineOp(op=name, text="Some text.", url="https://x.example/search")
        )
        assert "not an operation this file supports" not in " ".join(report.rejected), name


# -- the gate -----------------------------------------------------------------


def audit(text: str, verified=("https://x.example",)):
    from app.core.rules import audit_agents

    return audit_agents(text, site_url="https://x.example", verified_urls=list(verified))


def test_an_edit_that_introduces_an_unverified_url_is_refused():
    """AGT-004 is checked absolutely, not "newly failing".

    An edit must never leave a URL in the file that no probe confirmed, even if
    one was already there. This is the invariant, so it does not get graded on a
    curve.
    """
    from app.main import _regressions

    before = audit("# X\n\nHome: https://x.example\n")
    after = audit("# X\n\nHome: https://x.example\n\nAPI: https://invented.example/v1\n")

    broke = _regressions(before, after)

    assert broke
    assert "AGT-004" in broke[0]


def test_a_file_that_was_already_failing_still_cannot_be_made_worse():
    """The llms.txt gate compares error codes and lets an existing failure grow.

    "It was already a bit broken" is not permission to break it further.
    """
    from app.main import _regressions

    before = audit("# X\n\nA: https://one.invented.example\n")
    after = audit("# X\n\nA: https://one.invented.example\nB: https://two.invented.example\n")

    assert _regressions(before, after), "one more unverified URL is still a regression"


def test_a_harmless_prose_edit_passes_the_gate():
    from app.main import _regressions

    before = audit("# X\n\nAn agency.\n\nHome: https://x.example\n")
    after = audit("# X\n\nA Sydney SEO agency.\n\nHome: https://x.example\n")

    assert _regressions(before, after) == []


def test_removing_content_always_passes_the_gate():
    """Narrowing claims less than the evidence supports, which is always safe."""
    from app.main import _regressions

    before = audit("# X\n\nHome: https://x.example\n\nSearch: https://x.example\n")
    after = audit("# X\n\nHome: https://x.example\n")

    assert _regressions(before, after) == []


def test_an_info_level_failure_does_not_block_an_edit():
    """AGT-013 (no llms.txt pointer) is INFO. Blocking on it would mean no edit
    could ever be made to a site that has not published llms.txt yet."""
    from app.core.rules import AGENTS_BY_ID
    from app.core.rules.registry import Severity

    assert AGENTS_BY_ID["AGT-013"].severity is not Severity.ERROR
