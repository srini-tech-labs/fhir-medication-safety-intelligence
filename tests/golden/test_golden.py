"""Acceptance tests: all ten frozen patient scenarios (Phase 1 exit criteria).

Compared exactly: patient ID, overall severity/status, fired clinical rule IDs, data-gap rule IDs.
AI wording is never golden-tested.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import PACKAGE_DIR, load_json

EXPECTED = load_json(PACKAGE_DIR / "expected" / "expected_results.json")["patients"]
OVERRIDES = load_json(Path(__file__).with_name("expected_overrides.json"))["patients"]


def expected_for(patient_id: str) -> dict:
    base = next(p for p in EXPECTED if p["patientId"] == patient_id)
    return {**base, **{k: v for k, v in OVERRIDES.get(patient_id, {}).items() if k != "reason"}}


@pytest.mark.parametrize("patient_id", [p["patientId"] for p in EXPECTED])
def test_golden_case(container, patient_id):
    exp = expected_for(patient_id)
    result = container.analyses.run(patient_id)

    assert result.patient_id == exp["patientId"]
    assert result.status == "COMPLETED"
    assert result.overall_severity == exp["expectedOverallSeverity"]
    assert sorted(f.rule_id for f in result.findings) == sorted(exp["expectedFindingRuleIds"])
    assert sorted(g.rule_id for g in result.data_gaps) == sorted(exp["expectedDataGapRuleIds"])


def test_all_ten_patients_are_covered():
    assert [p["patientId"] for p in EXPECTED] == [f"P{n:03d}" for n in range(1, 11)]


def test_overrides_are_limited_to_the_approved_p006_data_gap():
    assert set(OVERRIDES) == {"P006"}
    assert set(OVERRIDES["P006"]) == {"expectedDataGapRuleIds", "reason"}
    assert EXPECTED[5]["expectedDataGapRuleIds"] == []  # frozen file itself is untouched


def test_p009_negative_control_has_no_findings_or_gaps(container):
    r = container.analyses.run("P009")
    assert (r.overall_severity, r.findings, r.data_gaps) == ("NONE", [], [])


def test_p010_reports_gap_without_fabricating_potassium(container):
    r = container.analyses.run("P010")
    assert r.overall_severity == "NEEDS_DATA" and r.findings == []
    (gap,) = r.data_gaps
    assert gap.status == "NEEDS_DATA" and gap.required_lab.loinc == "2823-3"
    snap = container.snapshots.snapshot("P010")
    assert snap.labs == []  # the note says a panel is pending; no value is invented


def test_p008_multiple_findings_are_ordered_by_severity(container):
    r = container.analyses.run("P008")
    assert [(f.rule_id, f.severity) for f in r.findings] == [("DL-001", "HIGH"), ("DDI-003", "MODERATE")]
    assert [f.finding_id for f in r.findings] == ["F-P008-001", "F-P008-002"]
    assert r.summary.model_dump() == {"total_findings": 2, "high": 1, "moderate": 1, "low": 0, "data_gaps": 0}


def test_p006_finding_and_data_gap_coexist_and_overall_stays_moderate(container):
    r = container.analyses.run("P006")
    assert r.overall_severity == "MODERATE"
    assert [f.rule_id for f in r.findings] == ["DDI-003"]
    assert [g.rule_id for g in r.data_gaps] == ["DG-001"]
