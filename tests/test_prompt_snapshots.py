"""Prompts are artifacts, so they get snapshotted like the rendered file.

A prompt is the highest-leverage untested string in the system -- one clause in
`summarise.py` produced 106 banned openers across a shipped file -- and it is
also the easiest thing to change without noticing. That happened here: an edit
to `build_user_message` silently matched nothing, ruff removed the import that
was its only remaining trace, and the whole suite stayed green because every
test asserted on behaviour rather than on the artifact.

Behavioural tests cannot close that gap. A prompt has no behaviour without a
model, so what the tests reached was the fallback path, which is exactly the
path the prompt does not affect. Snapshots close it by making the artifact
itself the thing under test.

Regenerate deliberately: `UPDATE_GOLDEN=1 pytest tests/test_prompt_snapshots.py`.
The same switch drives the rendered-output golden, so one habit covers both, and
an intended prompt change arrives in review as a diff rather than as silence.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.core.onboarding import SiteBrief
from app.llm.prompts import chat as chat_prompt
from app.llm.prompts import intent as intent_prompt
from app.llm.prompts import plan as plan_prompt
from app.llm.prompts import qa as qa_prompt
from app.llm.prompts import summarise as summarise_prompt
from app.llm.prompts import triage as triage_prompt

SNAPSHOTS = Path(__file__).parent / "fixtures" / "prompts"

BRIEF = SiteBrief(
    found_for="Digital PR and link building for Australian brands",
    audience="Marketing managers evaluating agencies; not developers",
    valuable=("/services/*", "/case-studies/*"),
    noise=("/tag/*",),
    embargoed=("/clients/acquisition-2026/*",),
)


def check(name: str, text: str) -> None:
    """Compare against the snapshot, or write it when regenerating."""
    SNAPSHOTS.mkdir(parents=True, exist_ok=True)
    path = SNAPSHOTS / f"{name}.txt"

    if os.environ.get("UPDATE_GOLDEN") == "1":
        path.write_text(text, encoding="utf-8", newline="")

    assert path.exists(), f"run once with UPDATE_GOLDEN=1 to create {path.name}"
    assert text == path.read_text(encoding="utf-8"), (
        f"{name} changed. If that was deliberate, regenerate with UPDATE_GOLDEN=1 "
        "and let the diff be reviewed."
    )


def test_plan_system_prompt() -> None:
    check("plan_system", plan_prompt.SYSTEM)


def test_plan_user_message_without_a_brief() -> None:
    check("plan_user_plain", plan_prompt.build_user_message("223 crawlable URLs", 400))


def test_plan_user_message_with_a_brief() -> None:
    """The one that would have caught the silent no-op."""
    check("plan_user_briefed", plan_prompt.build_user_message("223 crawlable URLs", 400, BRIEF))


def test_intent_system_prompt() -> None:
    """Pinned because the distinction it draws is the whole design.

    The prompt asks what a group *is* and forbids reasoning about what matters.
    A regeneration that softens that turns the classifier into an unmeasured
    importance judge, which is the failure the plan stage already shipped once.
    """
    check("intent_system", intent_prompt.SYSTEM)


def test_intent_user_message() -> None:
    from app.core.planning import build_planning_table
    from app.scrape.recon import RobotsInfo, SiteRecon

    urls = ["https://m.com/cars/make/" + str(i) for i in range(300)]
    recon = SiteRecon(
        site_url="https://m.com",
        robots=RobotsInfo(),
        urls=urls,
        url_sources=dict.fromkeys(urls, "AllNew_Make.xml"),
    )
    check("intent_user", intent_prompt.build_user_message(build_planning_table(recon)))


def test_the_intent_prompt_refuses_to_rank_importance() -> None:
    """It is asked what a group is, never what it is worth."""
    text = intent_prompt.SYSTEM.lower()

    assert "not whether it is important" in text
    assert "do not reason about value" in text


def test_summarise_site_prompt() -> None:
    check("summarise_site_system", summarise_prompt.SITE_SYSTEM)


def test_summarise_page_prompt() -> None:
    """The one that shipped 106 banned openers. Pinned hardest."""
    check("summarise_page_system", summarise_prompt.PAGE_SYSTEM)


def test_triage_system_prompt() -> None:
    check("triage_system", triage_prompt.SYSTEM)


def test_qa_system_prompt() -> None:
    check("qa_system", qa_prompt.SYSTEM)


def test_chat_system_prompt() -> None:
    check("chat_system", chat_prompt.SYSTEM)


# -- properties that must hold whatever the snapshot says --------------------
#
# A snapshot pins what the prompt *is*. These pin what it must never become, so
# regenerating a snapshot cannot quietly reintroduce a defect we have already
# paid for once.


def test_the_summarise_prompt_never_asks_for_a_verb_opener_again() -> None:
    """The clause that caused 106 Learn/Discover/Explore openers in a shipped file."""
    text = summarise_prompt.PAGE_SYSTEM.lower()
    assert "starting with a verb" not in text
    assert "start with a verb" not in text


def test_no_prompt_leaks_an_embargoed_path() -> None:
    """Withholding these is the point of the answer, not a nicety."""
    for name, text in [
        ("plan", plan_prompt.build_user_message("brief", 400, BRIEF)),
    ]:
        assert "acquisition-2026" not in text, name


@pytest.mark.parametrize(
    "text",
    [
        plan_prompt.SYSTEM,
        intent_prompt.SYSTEM,
        summarise_prompt.SITE_SYSTEM,
        summarise_prompt.PAGE_SYSTEM,
        triage_prompt.SYSTEM,
        qa_prompt.SYSTEM,
        chat_prompt.SYSTEM,
    ],
)
def test_system_prompts_are_not_accidentally_empty(text: str) -> None:
    """A prompt blanked by a bad edit is a silent quality collapse, not a failure."""
    assert len(text.strip()) > 200
