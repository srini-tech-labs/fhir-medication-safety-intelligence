"""Boundary and safety-property tests for the deterministic engine (no FHIR needed)."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.models.clinical import LabResult, MedicationOrder
from app.repository.local import LocalFHIRRepository
from app.rules.catalog import RuleCatalog
from app.rules.engine import RuleEngine
from tests.conftest import PACKAGE_DIR

AS_OF = date(2026, 9, 18)
LISINOPRIL, WARFARIN, IBUPROFEN = ("29046", "Lisinopril"), ("11289", "Warfarin"), ("5640", "Ibuprofen")
METFORMIN, INSULIN = ("6809", "Metformin"), ("274783", "Insulin glargine")


@pytest.fixture(scope="module")
def engine() -> RuleEngine:
    return RuleEngine(RuleCatalog.load(PACKAGE_DIR))


def med(drug, status="active") -> MedicationOrder:
    return MedicationOrder(f"MedicationRequest/{drug[1].lower()}", drug[1], drug[0], "x", status)


def lab(loinc, name, value, unit, days_ago=1) -> LabResult:
    return LabResult(f"Observation/{name.lower()}", name, loinc, value, unit, None,
                     AS_OF - timedelta(days=days_ago))


K = lambda v, d=1: lab("2823-3", "Potassium", v, "mmol/L", d)
EGFR = lambda v: lab("98979-8", "eGFR", v, "mL/min/{1.73_m2}")
GLU = lambda v: lab("2345-7", "Glucose", v, "mg/dL")
INR = lambda v: lab("6301-6", "INR", v, "{INR}")


def rule_ids(res):
    return [f.rule_id for f in res.findings]


@pytest.mark.parametrize("drug,make,edge,over,rule", [
    (LISINOPRIL, K, 5.7, 5.71, "DL-001"),
    (METFORMIN, EGFR, 30, 29.9, "DL-002"),
    (INSULIN, GLU, 54, 53.9, "DL-003"),
    (WARFARIN, INR, 4.0, 4.1, "DL-004"),
])
def test_thresholds_are_strict_and_boundary_does_not_fire(engine, drug, make, edge, over, rule):
    # DL-001/DL-004 use ">" (fire above); DL-002/DL-003 use "<" (fire below)
    fires, quiet = (over, edge)
    assert rule not in rule_ids(engine.evaluate("PX", [med(drug)], [make(quiet)], AS_OF))
    assert rule in rule_ids(engine.evaluate("PX", [med(drug)], [make(fires)], AS_OF))


def test_inactive_medication_is_ignored(engine):
    res = engine.evaluate("PX", [med(LISINOPRIL, "stopped")], [K(6.5)], AS_OF)
    assert res.findings == [] and res.data_gaps == [] and res.overall == "NONE"


def test_uses_most_recent_result_per_loinc(engine):
    labs = [K(6.5, d=30), K(4.5, d=1)]
    assert rule_ids(engine.evaluate("PX", [med(LISINOPRIL)], labs, AS_OF)) == []


def test_unit_mismatch_is_not_silently_converted(engine):
    res = engine.evaluate("PX", [med(LISINOPRIL)], [lab("2823-3", "Potassium", 6.5, "mg/dL")], AS_OF)
    assert res.findings == []


def test_data_gap_lookback_boundary_is_90_days(engine):
    assert engine.evaluate("PX", [med(LISINOPRIL)], [K(4.5, d=90)], AS_OF).data_gaps == []
    (gap,) = engine.evaluate("PX", [med(LISINOPRIL)], [K(4.5, d=91)], AS_OF).data_gaps
    assert gap.rule_id == "DG-001"


def test_data_gap_only_for_lisinopril(engine):
    assert engine.evaluate("PX", [med(WARFARIN)], [], AS_OF).data_gaps == []


def test_overall_precedence(engine):
    # finding + gap -> finding severity wins; NEEDS_DATA only when there is no finding
    both = engine.evaluate("PX", [med(LISINOPRIL), med(IBUPROFEN)], [], AS_OF)
    assert both.overall == "MODERATE" and both.summary.data_gaps == 1
    assert engine.evaluate("PX", [med(LISINOPRIL)], [], AS_OF).overall == "NEEDS_DATA"


def test_provenance_is_complete_on_a_drug_lab_finding(engine):
    (f,) = engine.evaluate("PX", [med(LISINOPRIL)], [K(5.8)], AS_OF).findings
    assert f.evidence.medications[0].rx_cui == "29046"
    assert f.evidence.labs[0].loinc == "2823-3" and f.evidence.labs[0].value == 5.8
    assert (f.provenance.trigger.operator, f.provenance.trigger.threshold) == (">", 5.7)
    assert f.provenance.fhir_resources == ["MedicationRequest/lisinopril", "Observation/potassium"]
    assert f.source.evidence_ids == ["EVID-002"] and f.provenance.evidence[0].source_url.startswith("https://")


def test_two_evidence_sources_are_classified_separately(engine):
    (f,) = engine.evaluate("PX", [med(INSULIN)], [GLU(52)], AS_OF).findings
    assert [e.evidence_id for e in f.provenance.evidence] == ["EVID-005", "EVID-006"]
    assert [e.provider for e in f.provenance.evidence] == ["DailyMed", "American Diabetes Association"]


def test_notes_cannot_create_findings(settings):
    """P006's note mentions ibuprofen. Remove the structured ibuprofen order: no DDI-003 may appear."""
    from app.container import build_container
    from app.services.explanation.mock import MockExplanationService

    class NoIbuprofenRepo(LocalFHIRRepository):
        def get_medications(self, patient_id):
            return [m for m in super().get_medications(patient_id) if m.rx_cui != "5640"]

    repo = NoIbuprofenRepo(settings.package_dir, settings.output_dir)
    assert "ibuprofen" in repo.get_documents("P006")[0].text.lower()  # the note still says so
    result = build_container(settings, repository=repo, explainer=MockExplanationService()).analyses.run("P006")
    assert [f.rule_id for f in result.findings] == []
