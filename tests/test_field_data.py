"""Hosted Lighthouse and Chrome UX Report, and the honesty rules around them.

Two of the six checks that needed a person can now be measured. That is only an
improvement if the numbers carry where they came from -- a lab run and a
real-user p75 are different measurements, and an origin-wide p75 is not a fact
about a page. Most of this file guards those distinctions.
"""

from __future__ import annotations

from app.scrape.crux import Basis, CruxResult, Direction, Trend
from app.scrape.pagespeed import CLS_GOOD, LighthouseFindings, Metric, _read, _tap_targets
from app.scrape.pagespeed import Basis as LabBasis
from app.scrape.readiness import CheckState, _cls_verdict, _from_browser

# -- the audit id moved -------------------------------------------------------


def test_the_current_lighthouse_tap_target_audit_is_found():
    """`tap-targets` was removed in Lighthouse 13; `target-size` replaced it.

    [measured 2026-08-24 against 13.4.1] The component's own verify string still
    quotes the old name, which is why this resolves ids rather than trusting it.
    """
    ok, detail = _tap_targets({"target-size": {"score": 0, "displayValue": "3 too small"}})

    assert ok is False
    assert "target-size" in detail


def test_the_older_audit_id_still_works():
    """A rollback must not silently stop measuring."""
    ok, _ = _tap_targets({"tap-targets": {"score": 1}})

    assert ok is True


def test_no_tap_target_audit_at_all_is_undecided_not_a_pass():
    ok, detail = _tap_targets({})

    assert ok is None
    assert "no tap-target audit" in detail


def test_an_audit_that_did_not_apply_is_undecided_not_a_pass():
    """Lighthouse reports null for an audit it could not run."""
    ok, _ = _tap_targets({"target-size": {"score": None}})

    assert ok is None


# -- lab versus field ---------------------------------------------------------


def test_field_data_outranks_a_lab_run_even_of_the_exact_page():
    """ "CLS under 0.1" is a claim about what people experienced."""
    lab = LighthouseFindings(
        url="https://x.example/",
        cls=Metric(value=0.4, basis=LabBasis.LAB),
    )
    field = CruxResult(cls_p75=0.02, basis=Basis.FIELD_ORIGIN)

    state, detail = _cls_verdict([lab], field, CLS_GOOD)

    assert state is CheckState.PASS, "the field number decides, not the lab one"
    assert "0.02" in detail
    assert "real users" in detail


def test_a_lab_number_says_it_is_lab_only():
    lab = LighthouseFindings(url="https://x.example/", cls=Metric(value=0.02, basis=LabBasis.LAB))

    _state, detail = _cls_verdict([lab], None, CLS_GOOD)

    assert "lab only" in detail
    assert "no field data" in detail


def test_origin_wide_field_data_does_not_claim_to_be_the_page():
    """The distinction that made a direct CrUX query worth building."""
    field = CruxResult(cls_p75=0.01, basis=Basis.FIELD_ORIGIN)

    _state, detail = _cls_verdict([], field, CLS_GOOD)

    assert "whole site" in detail
    assert "this page" not in detail


def test_page_level_field_data_says_so():
    field = CruxResult(cls_p75=0.01, basis=Basis.FIELD_URL)

    _state, detail = _cls_verdict([], field, CLS_GOOD)

    assert "this page" in detail


def test_nothing_measured_is_undecided_rather_than_a_pass():
    """A CLS of 0 is a perfect score, so defaulting to it would report the best
    possible result for a site nobody looked at."""
    state, _ = _cls_verdict([], None, CLS_GOOD)

    assert state is None


def test_an_unreachable_pagespeed_run_does_not_count_as_evidence():
    broken = LighthouseFindings(url="https://x.example/", error="ReadTimeout")

    assert _cls_verdict([broken], None, CLS_GOOD)[0] is None


def test_a_failing_field_number_names_the_threshold():
    field = CruxResult(cls_p75=0.25, basis=Basis.FIELD_ORIGIN)

    state, detail = _cls_verdict([], field, CLS_GOOD)

    assert state is CheckState.FAIL
    assert "0.25" in detail and "0.1" in detail


# -- the trend ----------------------------------------------------------------


def test_movement_smaller_than_the_material_change_is_steady():
    """The first version called prosperitymedia's 0.00 -> 0.01 "worsening".

    Real movement in the numbers, noise against a 0.1 budget. Deriving the
    threshold from the observed spread is what produced that, so it is passed in
    from the metric instead.
    """
    drift = Trend(values=tuple([0.0] * 6 + [0.01] * 19), material=0.02)

    assert drift.direction is Direction.STEADY


def test_a_real_regression_is_still_reported():
    regression = Trend(values=tuple([0.02] * 12 + [0.12] * 12), material=0.02)

    assert regression.direction is Direction.WORSENING


def test_a_real_improvement_is_reported():
    fixed = Trend(values=tuple([0.15] * 12 + [0.02] * 12), material=0.02)

    assert fixed.direction is Direction.IMPROVING


def test_too_little_history_is_no_trend_rather_than_steady():
    """ "Steady" is a claim. Four data points cannot support one."""
    assert Trend(values=(0.01, 0.02, 0.01, 0.02), material=0.02).direction is None
    assert "not enough history" in Trend(values=(0.01,), material=0.02).describe()


def test_a_trend_is_read_from_quarters_not_endpoints():
    """Two endpoints on a noisy weekly series is a coin toss."""
    spike_at_the_end = Trend(values=tuple([0.02] * 23 + [0.30, 0.02]), material=0.02)

    assert spike_at_the_end.direction is Direction.STEADY


# -- parsing ------------------------------------------------------------------


def test_a_response_with_no_field_block_falls_back_to_the_lab_metric():
    findings = _read(
        "https://x.example/",
        {
            "lighthouseResult": {
                "lighthouseVersion": "13.4.1",
                "audits": {"cumulative-layout-shift": {"numericValue": 0.0007}},
            }
        },
    )

    assert findings.cls.basis is LabBasis.LAB
    assert findings.cls.value == 0.0007


def test_crux_percentiles_are_scaled_back_from_integers():
    """CrUX multiplies CLS by 100 so it can stay an integer."""
    findings = _read(
        "https://x.example/",
        {
            "loadingExperience": {"metrics": {"CUMULATIVE_LAYOUT_SHIFT_SCORE": {"percentile": 12}}},
            "lighthouseResult": {"audits": {}},
        },
    )

    assert findings.cls.value == 0.12
    assert findings.cls.basis is LabBasis.FIELD


def test_an_unmeasured_metric_describes_itself_as_such():
    assert Metric(value=None, basis=LabBasis.LAB).describe() == "not measured"
    assert Metric(value=None, basis=LabBasis.LAB).measured is False


# -- the dispatcher -----------------------------------------------------------


def test_tap_targets_reports_how_many_pages_failed():
    findings = [
        LighthouseFindings(url="https://x.example/a", tap_targets_ok=False, tap_targets_detail="t"),
        LighthouseFindings(url="https://x.example/b", tap_targets_ok=True),
        LighthouseFindings(url="https://x.example/c", tap_targets_ok=False),
    ]

    state, detail = _from_browser("tap-targets", findings)

    assert state is CheckState.FAIL
    assert "2 of 3" in detail


def test_tap_targets_passes_only_when_every_checked_page_passed():
    findings = [LighthouseFindings(url="https://x.example/a", tap_targets_ok=True)]

    state, _ = _from_browser("tap-targets", findings)

    assert state is CheckState.PASS
