"""The client view model, tested as a boundary rather than as a renderer.

A string blacklist ("assert 'FULL-003' not in html") is the obvious way to test
this and the wrong one: it passes the day someone rewords a message, and it only
ever proves that one render happened not to print one string. These tests prove
the stronger property -- that the internals are *not reachable*.

Two layers do the work. The type-closure test walks the built object and asserts
every leaf is a primitive, so a template cannot reach `.component.key` because
there is no `.component`. The taint test puts a unique sentinel in every
internal-only field, derived by walking `dataclasses.fields()`, so adding a
field upstream without deciding whether a client may see it fails here rather
than shipping.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import fields, is_dataclass
from datetime import date, datetime

import pytest

from app.core.client_report import (
    MAX_EXAMPLES,
    MAX_FINDINGS,
    REPORT_SECTIONS,
    SECTION_KEYS,
    ClientReport,
    Standing,
    build_client_report,
    section_title,
)
from app.core.components import COMPONENTS, ComponentState, Family, SiteType
from app.core.site_state import ComponentStatus, SiteStatus

SITE_TYPE = SiteType.CONTENT
OPERATOR = "someone@prosperitymedia.com.au"

# Types that must never appear anywhere in a built report.
FORBIDDEN = ("Component", "ComponentStatus", "SiteStatus", "SiteView", "Report", "Finding")
ALLOWED_LEAVES = (str, int, float, bool, date, datetime, type(None))


# -- fakes ---------------------------------------------------------------------


class FakeFinding:
    def __init__(self, rule_id="FULL-003", message="Body headings were not demoted.", count=3):
        self.rule_id = rule_id
        self.message = message
        self.count = count
        self.examples = ["**What is Digital PR?**", "**Second**", "**Third**", "**Fourth**"]
        self.reason = "internal skip reason"


class FakeReport:
    def __init__(self, score=45, failures=None):
        self.score = score
        self.failures = failures if failures is not None else [FakeFinding()]
        self.capped_by = "error"
        self.skipped = []


class FakeReadiness:
    score = 53


class FakeView:
    """Only the attributes `build_client_report` actually reads."""

    def __init__(self, domain="example.com"):
        self.domain = domain
        self.site_url = f"https://{domain}"
        self.readiness = FakeReadiness()
        self.checked_ago = "2 days"
        self.is_stale = True


def _status(site_type=SITE_TYPE) -> SiteStatus:
    """A realistic spread: one live, one probe-failed, one marked, one missing."""
    status = SiteStatus(site_url="https://example.com", site_type=site_type)
    for index, component in enumerate(COMPONENTS):
        if component.applies_to(site_type).name == "NO":
            state, detail, decided, artifact = (
                ComponentState.NOT_APPLICABLE,
                "not expected on this site",
                False,
                "",
            )
        elif index % 4 == 0:
            state, detail, decided, artifact = ComponentState.LIVE, "200, text/plain", True, ""
        elif index % 4 == 1:
            # The leak: derive writes this for a manual mark, and it is never
            # probe-decided.
            state, detail, decided, artifact = (
                ComponentState.LIVE,
                f"marked done by {OPERATOR}",
                False,
                "",
            )
        elif index % 4 == 2 and component.artifact:
            state, detail, decided, artifact = (
                ComponentState.READY,
                "generated and ready to publish",
                False,
                component.artifact,
            )
        else:
            state, detail, decided, artifact = ComponentState.MISSING, "404", True, ""
        status.statuses.append(
            ComponentStatus(
                component,
                state,
                detail,
                artifact_name=artifact,
                verify=component.verify,
                probe_decided=decided,
            )
        )
    return status


def _reports() -> dict[str, object]:
    return {s.key: FakeReport() for s in _status().statuses if s.artifact_name}


def _built(section="report") -> ClientReport:
    return build_client_report(
        FakeView(), _status(), section, client_name="Example Ltd", reports=_reports()
    )


# -- layer 1: type closure -----------------------------------------------------


def _walk(value, path="report"):
    """Every (path, value) leaf in the object graph."""
    if is_dataclass(value) and not isinstance(value, type):
        for f in fields(value):
            yield from _walk(getattr(value, f.name), f"{path}.{f.name}")
    elif isinstance(value, (tuple, list)):
        for index, item in enumerate(value):
            yield from _walk(item, f"{path}[{index}]")
    else:
        yield path, value


def test_the_report_carries_only_primitives():
    """The load-bearing test.

    Proves the template *cannot reach* an internal, which is categorically
    stronger than proving one render happened not to print one. It also fails the
    moment someone adds a convenience field like `raw: ComponentStatus`.
    """
    offenders = [
        f"{path} = {type(value).__name__}"
        for path, value in _walk(_built())
        if not isinstance(value, ALLOWED_LEAVES) and not isinstance(value, Standing)
    ]

    assert offenders == [], f"non-primitive leaves reached the client model: {offenders}"


def test_no_internal_type_is_reachable_from_the_report():
    names = {type(value).__name__ for _path, value in _walk(_built())}

    assert not (names & set(FORBIDDEN)), f"internal types reachable: {names & set(FORBIDDEN)}"


# -- layer 2: the taint round-trip ---------------------------------------------


def _taint(status: SiteStatus) -> dict[str, str]:
    """Put a unique sentinel in every internal-only field.

    Derived by naming the fields rather than by guessing at strings, so this
    stays correct when the product copy is reworded -- which a list of literal
    strings would not.
    """
    marks: dict[str, str] = {}

    def mark(where: str) -> str:
        token = f"SENTINEL{uuid.uuid4().hex}"
        marks[where] = token
        return token

    for s in status.statuses:
        # `detail` is tainted only where no probe decided it. A probe-decided
        # detail is evidence about the client's own site and is carried through
        # on purpose -- tainting it here would assert the opposite of the design.
        # The three leak paths are exactly the undecided ones: a manual mark
        # carrying an operator email, a MANUAL check carrying a verify command,
        # and `derive`'s internal fallback strings.
        if not s.probe_decided:
            s.detail = mark(f"detail:{s.key}")
        s.template_body = mark(f"template_body:{s.key}")
        s.verify = mark(f"verify:{s.key}")
    return marks


def test_no_internal_field_reaches_a_client_render(monkeypatch):
    """A blacklist of fields, not of strings.

    `verify` is exempted only for the handover, which is the one section whose
    declared reader is the client's own developer.
    """
    status = _status()
    sentinels = _taint(status)
    view = FakeView()

    for section in ("overview", "checklist", *[f.value for f in Family]):
        report = build_client_report(view, status, section, reports={})
        blob = repr(report)
        leaked = sorted(k for k, token in sentinels.items() if token in blob)
        assert leaked == [], f"{section} leaked: {leaked}"


def test_a_probe_decided_detail_is_the_one_thing_that_survives():
    """Evidence is carried, bookkeeping is not, and the predicate is the same one."""
    status = SiteStatus(site_url="https://example.com", site_type=SITE_TYPE)
    component = next(c for c in COMPONENTS if c.family is Family.CRAWL)
    status.statuses = [
        ComponentStatus(component, ComponentState.LIVE, "200, text/plain", probe_decided=True)
    ]

    item = (
        build_client_report(FakeView(), status, "crawl", reports={}).sections[0].groups[0].items[0]
    )

    assert item.evidence == "200, text/plain"


def test_an_operator_email_never_reaches_a_client():
    """The leak this module exists to close.

    `derive` writes "marked done by <email>" into `detail` for a manual mark, and
    the staff partial renders `detail` unconditionally.
    """
    status = SiteStatus(site_url="https://example.com", site_type=SITE_TYPE)
    component = next(c for c in COMPONENTS if c.family is Family.CRAWL)
    status.statuses = [
        ComponentStatus(
            component, ComponentState.LIVE, f"marked done by {OPERATOR}", probe_decided=False
        )
    ]

    report = build_client_report(FakeView(), status, "crawl", reports={})

    assert OPERATOR not in repr(report)
    assert report.sections[0].groups[0].items[0].evidence == ""


def test_a_manual_check_does_not_pass_its_own_command_off_as_evidence():
    """MANUAL puts the verify command in `detail`. A command is an instrument."""
    status = SiteStatus(site_url="https://example.com", site_type=SITE_TYPE)
    component = next(c for c in COMPONENTS if c.family is Family.PAGE)
    status.statuses = [
        ComponentStatus(
            component,
            ComponentState.MISSING,
            "npx lighthouse https://example.com --view",
            probe_decided=False,
        )
    ]

    item = (
        build_client_report(FakeView(), status, "page", reports={}).sections[0].groups[0].items[0]
    )

    assert item.evidence == ""


# -- rule identifiers ----------------------------------------------------------


def test_no_rule_identifier_survives():
    """Prefixes come from the registry, so a new rule family is covered by default."""
    from app.core.rules.agents_rules import AGENTS_RULES
    from app.core.rules.crawl_rules import CRAWL_RULES
    from app.core.rules.delivery_rules import CATALOG_RULES, HEADER_RULES
    from app.core.rules.full_rules import FULL_RULES
    from app.core.rules.index_rules import INDEX_RULES

    every = [*INDEX_RULES, *FULL_RULES, *AGENTS_RULES, *CRAWL_RULES, *HEADER_RULES, *CATALOG_RULES]
    prefixes = sorted({rule.id.split("-")[0] for rule in every})
    pattern = re.compile(rf"\b({'|'.join(prefixes)})-\d+\b")

    assert pattern.search(repr(_built())) is None, "a rule id reached the client model"


def test_the_finding_message_does_survive():
    """Or the test above would pass by carrying nothing at all."""
    checks = [
        item.check
        for section in _built().sections
        for group in section.groups
        for item in group.items
        if item.check
    ]

    assert checks, "no artifact carried a check"
    assert any(c.findings and c.findings[0].message for c in checks)


def test_a_score_is_never_shown_as_a_number():
    """`82/100` invites "why not 100?", and the answer is the ids just removed."""
    bands = {
        item.check.band
        for section in _built().sections
        for group in section.groups
        for item in group.items
        if item.check
    }

    assert bands and all(not re.search(r"\d+\s*/\s*100", band) for band in bands)


# -- caps ----------------------------------------------------------------------


def test_examples_and_findings_are_capped_and_the_remainder_is_counted():
    """Truncating silently would be the inflation the rule engine exists to stop."""
    report = build_client_report(
        FakeView(),
        _status(),
        "checklist",
        reports={
            s.key: FakeReport(failures=[FakeFinding() for _ in range(MAX_FINDINGS + 4)])
            for s in _status().statuses
        },
    )
    checks = [i.check for g in report.sections[0].groups for i in g.items if i.check]

    assert checks
    for check in checks:
        assert len(check.findings) <= MAX_FINDINGS
        assert check.withheld == 4
        assert all(len(f.examples) <= MAX_EXAMPLES for f in check.findings)


# -- sections ------------------------------------------------------------------


def test_the_combined_report_covers_every_applicable_component_exactly_once():
    """The composition decision, held by a test.

    `for_client()` and `for_developer()` partition the applicable set, so the
    report covers each item once. Overview plus the six families would list
    everything twice.
    """
    status = _status()
    titles = [
        item.title
        for section in build_client_report(FakeView(), status, "report", reports={}).sections
        for group in section.groups
        for item in group.items
    ]
    applicable = {
        s.component.title for s in status.statuses if s.state is not ComponentState.NOT_APPLICABLE
    }

    assert sorted(titles) == sorted(applicable)


def test_the_report_is_the_three_sections_it_says_it_is():
    keys = [s.key for s in _built().sections]

    assert tuple(keys) == REPORT_SECTIONS


def test_not_applicable_items_are_dropped_rather_than_labelled():
    """A page of "does not apply" reads as a broken tool, not as a decision."""
    standings = {
        item.standing
        for section in _built().sections
        for group in section.groups
        for item in group.items
    }

    assert standings <= {Standing.DONE, Standing.PREPARED, Standing.OUTSTANDING}


def test_verify_is_carried_into_the_handover_and_nowhere_else():
    view, status = FakeView(), _status()

    handover = build_client_report(view, status, "handover", reports={})
    others = [
        build_client_report(view, status, key, reports={})
        for key in ("overview", "checklist", *[f.value for f in Family])
    ]

    assert any(item.how_to_check for g in handover.sections[0].groups for item in g.items), (
        "the developer needs a way to confirm the fix landed"
    )
    assert not [
        item.how_to_check
        for report in others
        for section in report.sections
        for group in section.groups
        for item in group.items
        if item.how_to_check
    ]


def test_an_artifact_is_named_but_never_linked():
    items = [
        item
        for section in _built().sections
        for group in section.groups
        for item in group.items
        if item.file_name
    ]

    assert items, "nothing was generated in this fixture"
    assert all("/agents/download" not in repr(item) for item in items)
    assert all(item.serve_at for item in items)


@pytest.mark.parametrize("section", SECTION_KEYS)
def test_every_section_key_builds(section):
    report = build_client_report(FakeView(), _status(), section, reports={})

    assert report.title == section_title(section)
    assert report.sections


def test_an_unknown_section_is_refused():
    with pytest.raises(ValueError, match="unknown section"):
        build_client_report(FakeView(), _status(), "../../etc/passwd", reports={})


def test_an_empty_family_says_why_rather_than_showing_nothing():
    status = SiteStatus(site_url="https://example.com", site_type=SITE_TYPE)
    status.statuses = [
        ComponentStatus(c, ComponentState.NOT_APPLICABLE, "n/a")
        for c in COMPONENTS
        if c.family is Family.CAPABILITIES
    ]

    section = build_client_report(FakeView(), status, "capabilities", reports={}).sections[0]

    assert section.groups == ()
    assert section.empty_note


def test_the_client_name_falls_back_to_the_domain():
    assert build_client_report(FakeView(), _status(), "overview", reports={}).client_name == (
        "example.com"
    )


def test_the_age_of_the_data_travels_with_it():
    """A client reading a month-old audit should be able to tell."""
    report = build_client_report(FakeView(), _status(), "overview", reports={})

    assert report.checked_ago == "2 days"
    assert report.is_stale is True


def test_an_item_already_in_place_says_nothing_further():
    """The pill already says "In place".

    Repeating "nothing to do" under every one of them is a column of noise a
    reader learns to skip, which is a bad habit to teach in a document whose
    other rows matter.
    """
    status = SiteStatus(site_url="https://example.com", site_type=SITE_TYPE)
    component = next(c for c in COMPONENTS if c.family is Family.CRAWL)
    status.statuses = [
        ComponentStatus(component, ComponentState.LIVE, "200, text/plain", probe_decided=True)
    ]

    item = (
        build_client_report(FakeView(), status, "crawl", reports={}).sections[0].groups[0].items[0]
    )

    assert item.standing is Standing.DONE
    assert item.what_to_do == ""


def test_an_item_not_in_place_does_say_what_happens_next():
    """Or the test above would be satisfied by a model that never advises anyone."""
    status = SiteStatus(site_url="https://example.com", site_type=SITE_TYPE)
    component = next(c for c in COMPONENTS if c.family is Family.CRAWL)
    status.statuses = [
        ComponentStatus(component, ComponentState.MISSING, "404", probe_decided=True)
    ]

    item = (
        build_client_report(FakeView(), status, "crawl", reports={}).sections[0].groups[0].items[0]
    )

    assert item.what_to_do
