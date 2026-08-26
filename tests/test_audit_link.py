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
