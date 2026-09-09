"""Joining a Checker audit to the components this tool can fix.

The join is by path, because both sides already speak it: the Checker's
`llm_result` is keyed by the path it probed and `Component.path` is the same
shape. Nothing is inferred from wording -- the recommendation `pillar` field is a
display label that takes values like "Cloudflare" which are not pillars at all,
so matching on it would silently mis-file findings.
"""

from __future__ import annotations

import pytest

from app.core.audit_link import (
    AUDIT_PATHS,
    PATH_DISAGREEMENTS,
    AuditFinding,
    link_audit,
    path_disagreements,
)
from app.core.components import by_key

EXPORT = {
    "domain": "www.nrma.com.au",
    "overall_score": 32,
    "overall_grade": "F",
    "rubric_version": 4,
    "pillar_scores": {"robots_crawl": 61},
    "recommendations": [
        {"severity": "warn", "pillar": "AI Discoverability", "text": "No AI guidance files."},
        {"severity": "error", "pillar": "JS Rendering", "text": "Content needs JavaScript."},
        {"severity": "error", "pillar": "Robots & Crawl", "text": "Cloudflare blocks GPTBot."},
        {"severity": "warn", "pillar": "Schema & Entity", "text": "Products have no GTIN."},
    ],
    "llm_result": {
        "raw_data": {
            "llm_txt": {
                "/llms.txt": {"found": False},
                "/llm.txt": {"found": True},
                "/agents.md": {"found": False},
            },
            "wellknown": {"/.well-known/ucp": {"found": True}},
        }
    },
}


# -- the mapping is real ----------------------------------------------------------


@pytest.mark.parametrize("path,key", sorted(AUDIT_PATHS.items()))
def test_every_mapped_path_names_a_component_that_exists(path, key):
    """A typo here would silently drop a whole pillar's worth of evidence."""
    assert by_key(key) is not None, f"{path} maps to {key!r}, which is not a component"


def test_the_two_tools_disagree_about_two_paths_and_it_is_reported():
    """Not resolved in the join, on purpose.

    One of each pair is wrong. Picking a winner inside a mapping function would
    hide a conflict between two published opinions, and whichever tool a client
    happened to run would decide where their developer was told to put the file.

    This test exists to keep the disagreement visible. When somebody settles it,
    it fails and points at the line to delete.
    """
    live = path_disagreements()

    assert live == {
        "a2a-card": ("/.well-known/agent-card.json", "/.well-known/agent.json"),
        "mcp-card": ("/.well-known/mcp.json", "/.well-known/mcp/server-card.json"),
    }, f"the path conflict changed: {live}"


def test_the_disagreement_table_only_lists_real_conflicts():
    """If `components.py` is aligned with the Checker, this stops reporting it."""
    for key, (theirs, _) in PATH_DISAGREEMENTS.items():
        component = by_key(key)
        assert component is not None
        if component.path == theirs:
            assert key not in path_disagreements()


# -- surfaces --------------------------------------------------------------------


def test_a_file_found_under_any_of_its_names_counts_as_found():
    """`/llms.txt` and `/llm.txt` are one file the ecosystem has not named.

    Reporting "missing" because we looked under the name the site did not choose
    would be a finding about naming, not about the site.
    """
    view = link_audit(EXPORT)

    assert view.surfaces["llms-txt"] is True


def test_a_file_the_checker_did_not_find_is_recorded_as_absent():
    view = link_audit(EXPORT)

    assert view.surfaces["agents-md"] is False


def test_a_path_the_checker_never_probed_is_absent_not_false():
    """Absence of evidence, kept distinct from evidence of absence.

    A key missing from `surfaces` means the Checker did not look. Defaulting it
    to `False` would report a file as missing on the strength of nobody having
    checked -- the exact error the rest of this tool is built to avoid.
    """
    view = link_audit(EXPORT)

    assert "llms-full" not in view.surfaces


# -- findings --------------------------------------------------------------------


def test_findings_are_split_by_whether_we_can_generate_a_file():
    """35% of the weighted rubric maps onto files we produce; 65% does not.

    The split has to be visible, or the tool implies it can fix everything.
    """
    view = link_audit(EXPORT)

    assert [f.pillar for f in view.actionable] == ["Robots & Crawl", "AI Discoverability"]
    assert {f.pillar for f in view.for_developer} == {"JS Rendering", "Schema & Entity"}


def test_errors_sort_above_warnings():
    view = link_audit(EXPORT)

    assert [f.severity for f in view.findings] == ["error", "error", "warn", "warn"]


def test_an_unknown_severity_sorts_last_rather_than_crashing():
    """A new severity upstream must not take the panel down."""
    view = link_audit({"recommendations": [{"severity": "nightmare", "pillar": "X", "text": "t"}]})

    assert view.findings[0].rank == 2
    assert not view.findings[0].is_error


def test_the_recommendation_text_is_carried_verbatim():
    """It is prose written by the Checker and never parsed for meaning."""
    view = link_audit(EXPORT)

    assert any(f.text == "Cloudflare blocks GPTBot." for f in view.findings)


def test_a_pillar_label_that_is_not_a_pillar_is_handled():
    """The Checker labels some recommendations "Cloudflare", which is not a
    pillar. It must land in the developer group rather than vanish."""
    view = link_audit(
        {"recommendations": [{"severity": "error", "pillar": "Cloudflare", "text": "t"}]}
    )

    assert len(view.for_developer) == 1


# -- tolerance -------------------------------------------------------------------


def test_an_empty_export_produces_an_empty_view_rather_than_raising():
    view = link_audit({})

    assert view.findings == []
    assert view.surfaces == {}
    assert view.overall_score is None


def test_a_malformed_recommendation_is_skipped_not_fatal():
    """The score and the surfaces are still worth having."""
    view = link_audit(
        {"overall_score": 50, "recommendations": ["not a dict", {"severity": "warn"}, None]}
    )

    assert view.findings == []
    assert view.overall_score == 50


def test_a_missing_score_is_none_and_never_zero():
    """A site that scored 0 and a site nobody scored are different findings."""
    assert link_audit({}).overall_score is None
    assert link_audit({"overall_score": 0}).overall_score == 0
    assert link_audit({"overall_score": "n/a"}).overall_score is None


def test_the_domain_falls_back_to_the_payload():
    assert link_audit(EXPORT).domain == "www.nrma.com.au"
    assert link_audit(EXPORT, domain="nrma.com.au").domain == "nrma.com.au"


def test_findings_group_by_pillar_worst_first():
    grouped = link_audit(EXPORT).by_pillar()

    assert list(grouped) == [
        "JS Rendering",
        "Robots & Crawl",
        "AI Discoverability",
        "Schema & Entity",
    ]


def test_a_finding_is_immutable():
    """It is a record of what a third party said, not a working value."""
    finding = AuditFinding(severity="error", pillar="X", text="t")

    with pytest.raises(AttributeError):
        finding.text = "changed"


# -- how it renders ----------------------------------------------------------------


def _squash(html: str) -> str:
    """Collapse whitespace before matching.

    The panel's prose wraps at 80 columns, so a phrase that reads as one line in
    the file arrives with a newline and eight spaces in the middle of it. A test
    that fails on the line width rather than on the wording is a test that
    punishes editing the template.
    """
    import re

    return re.sub(r"\s+", " ", html)


def _render_panel(**extra):
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
    from tests.test_nav import _render

    return _render("client_settings.html", exists=True, error=None, **extra)


def test_a_client_with_no_audit_says_so_rather_than_showing_a_zero():
    """Never audited is not a score of nothing, and must not read as one."""
    html = _render_panel(audit=None, audited_ago="", checker_url="")

    assert "No audit has been received" in html
    assert "0/100" not in html


def test_the_panel_says_who_measured_it():
    """The attribution rule, in the one place it is visible to a client.

    Nothing on this panel was measured here. A stale third-party claim rendered
    as our own live check is the worst thing this integration could introduce.
    """
    html = _render_panel(
        audit=link_audit(EXPORT), audited_ago="2 days ago", checker_url="https://checker.example"
    )

    squashed = _squash(html)

    assert "LLM Access Checker" in squashed
    assert "not by this tool" in squashed
    assert "2 days ago" in squashed


def test_the_two_scores_are_shown_apart_and_never_averaged():
    """Ours is priority-weighted over what we checked; theirs is pillar-weighted
    over their rubric. Merging them would invent a number neither tool computed."""
    html = _render_panel(audit=link_audit(EXPORT), audited_ago="", checker_url="", readiness=53)

    assert "32/100" in html, "the Checker's score is missing"
    assert "53/100" in html, "our readiness is missing"


def test_the_rubric_version_is_shown_with_the_score():
    """The Checker refuses to trend across versions; a bare number invites it."""
    html = _render_panel(audit=link_audit(EXPORT), audited_ago="", checker_url="")

    squashed = _squash(html)

    assert "v4" in squashed
    assert "not comparable" in squashed


def test_findings_render_in_two_groups():
    html = _render_panel(audit=link_audit(EXPORT), audited_ago="", checker_url="")

    assert "What we can generate for you" in html
    assert "What needs a developer" in html
    assert "Content needs JavaScript." in html, "a developer finding was dropped"


def test_an_audit_with_no_recommendations_does_not_read_as_clean():
    """Silence from the Checker is not a pass, and saying nothing implies one."""
    html = _render_panel(audit=link_audit({"overall_score": 70}), audited_ago="", checker_url="")

    assert "not the same as a clean result" in _squash(html)


# -- the breakdown that was ingested and never shown -----------------------------


def test_the_rubric_is_broken_out_by_pillar():
    """`pillar_scores` arrived on every audit and was read by nothing.

    An overall 61 cannot tell an operator whether the site is strong where we can
    help and weak where we cannot, or the reverse -- and those are opposite
    conversations to have with a client.
    """
    from app.core.audit_link import link_audit

    view = link_audit(
        {
            "overall_score": 61,
            "pillar_scores": {"robots_crawl": 61, "schema_entity": 12},
        }
    )
    by_slug = {p.slug: p for p in view.pillars()}

    assert by_slug["robots_crawl"].score == 61
    assert by_slug["schema_entity"].score == 12
    assert by_slug["robots_crawl"].generated is True
    assert by_slug["schema_entity"].generated is False


def test_a_pillar_the_export_did_not_score_is_not_a_zero():
    """The nav's `gap: int | None` rule, applied to a second source: a pillar
    nobody scored must not read as a pillar that scored nothing."""
    from app.core.audit_link import link_audit

    view = link_audit({"rubric_version": 5, "pillar_scores": {"robots_crawl": 61}})
    unscored = next(p for p in view.pillars() if p.slug == "js_rendering")

    assert unscored.score is None
    assert unscored.measured is False


def test_the_whole_rubric_is_shown_even_when_half_of_it_is_unscored():
    """Two of sixteen rows reads as a two-pillar rubric, which understates what
    the client is being graded on. The missing ones render as "not scored"."""
    from app.core.audit_link import PILLAR_WEIGHTS, link_audit

    view = link_audit({"rubric_version": 5, "pillar_scores": {"robots_crawl": 61}})

    assert len(view.pillars()) == len(PILLAR_WEIGHTS[5]) == 16
    assert sum(1 for p in view.pillars() if p.measured) == 1


def test_the_generated_share_is_stated_per_version():
    """The integration's whole risk is reading as though this tool fixes
    everything the Checker measures.

    It is 35 under v2 and about 22 under v5, because v5 split the content pillar
    five ways and none of the pieces is a file we produce. One number for both
    would be wrong for one of them.
    """
    from app.core.audit_link import link_audit

    assert link_audit({"rubric_version": 2}).generated_weight == 35
    assert link_audit({"rubric_version": 5}).generated_weight == 22


def test_the_v2_weights_are_a_whole_rubric():
    from app.core.audit_link import PILLAR_WEIGHTS

    assert sum(PILLAR_WEIGHTS[2].values()) == 100


def test_the_panel_renders_the_breakdown():
    from pathlib import Path

    markup = (
        Path(__file__).resolve().parents[1] / "templates" / "partials" / "audit_panel.html"
    ).read_text(encoding="utf-8")

    assert "audit.pillars()" in markup, "the breakdown is computed and not shown"
    assert "generated_weight" in markup


# -- the rubric moves, and a hardcoded copy of it goes stale ----------------------


V5_GLASSONS = {
    "overall_score": 50,
    "overall_grade": "C",
    "rubric_version": 5,
    "pillar_scores": {
        "robots_crawl": 81,
        "js_rendering": 34,
        "performance_crawlability": 86,
        "ai_discoverability": 13,
        "schema_entity": 59,
        "machine_readability": None,
    },
}


def test_a_v5_audit_renders_its_own_pillars():
    """The first version of this table hardcoded v2's six flat pillars and was
    two releases out of date when it shipped. Production runs v5 -- sixteen
    pillars under Crawl, Comprehend, Convince and Convert -- so every row would
    have rendered "not scored" against a live audit.

    Pillars are read from the payload now. A version we have never seen still
    renders.
    """
    from app.core.audit_link import link_audit

    labels = {p.label for p in link_audit(V5_GLASSONS).pillars()}

    assert "Performance & Crawlability" in labels, "a v5-only pillar was dropped"
    assert "Machine Readability" in labels


def test_the_heaviest_pillar_comes_first_where_we_know_the_weights():
    """The order is the order the work matters in."""
    from app.core.audit_link import link_audit

    pillars = link_audit(V5_GLASSONS).pillars()
    weights = [p.weight for p in pillars]

    assert weights == sorted(weights, reverse=True)
    assert pillars[0].label in {"Robots & Crawl", "JS Rendering"}


def test_an_unknown_rubric_version_shows_scores_without_inventing_weights():
    """A weight we have not got is not a weight of zero, and borrowing one from a
    different version is how the first table came to describe the wrong rubric."""
    from app.core.audit_link import link_audit

    future = link_audit({"rubric_version": 9, "pillar_scores": {"something_new": 42}})
    pillar = future.pillars()[0]

    assert pillar.score == 42
    assert pillar.weight is None
    assert pillar.label == "Something New", "an unmet pillar is humanised, not dropped"
    assert future.generated_weight is None


def test_markdown_serving_is_a_pillar_we_can_now_answer():
    """v5 scores it separately, and `md/` and `okf/` produce exactly that."""
    from app.core.audit_link import GENERATED_PILLARS, link_audit

    assert "machine_readability" in GENERATED_PILLARS

    ours = {p.label for p in link_audit(V5_GLASSONS).pillars() if p.generated}
    assert "Machine Readability" in ours


def test_the_weights_we_hold_are_per_version():
    from app.core.audit_link import PILLAR_WEIGHTS

    assert sum(PILLAR_WEIGHTS[2].values()) == 100
    # v5's are the stage share times the pillar share, rounded, so they land near
    # 100 rather than on it. Inventing precision the Checker does not claim would
    # be the same error as the version this replaced.
    assert 95 <= sum(PILLAR_WEIGHTS[5].values()) <= 105
