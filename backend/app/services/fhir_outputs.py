"""Maps deterministic results onto FHIR R4 DetectedIssue / RiskAssessment resources.

Shapes follow data/phase0_v1_0/fhir/examples/. RiskAssessment is a *qualitative* roll-up only:
this prototype has no validated predictive model, so no probability is ever asserted.
"""
from __future__ import annotations

from app.models.contract import Finding, OverallStatus

DATA_ORIGIN_TAG = {
    "system": "https://example.org/fhir/CodeSystem/data-origin",
    "code": "synthetic",
    "display": "Synthetic demonstration data",
}
RISK_LEVEL_SYSTEM = "https://example.org/fhir/CodeSystem/med-safety-risk-level"

_ISSUE_CODE = {"DRUG_DRUG": "Drug-drug safety concern", "DRUG_LAB": "Drug-lab safety concern"}
_RISK_DISPLAY = {"HIGH": "High", "MODERATE": "Moderate", "LOW": "Low", "NONE": "None identified",
                 "NEEDS_DATA": "Needs data"}
RISK_RATIONALE = (
    "Prototype qualitative summary derived from deterministic rule findings. "
    "No probability estimate is asserted."
)


# Written by SMART Medication Reconciliation Intelligence (provider dispositions); never produced here.
PROVIDER_OWNED_FIELDS = ("mitigation",)


def preserve_provider_fields(issue: dict, current: dict | None) -> dict:
    """Re-persisting replaces producer-owned content as before but carries provider-owned fields of the
    currently stored version forward unchanged."""
    carried = {k: current[k] for k in PROVIDER_OWNED_FIELDS if current and current.get(k)}
    return {**issue, **carried}


def version_etag(resource: dict | None) -> str | None:
    version = ((resource or {}).get("meta") or {}).get("versionId")
    return f'W/"{version}"' if version else None


def detected_issue_id(patient_id: str, rule_id: str) -> str:
    return f"di-{patient_id.lower()}-{rule_id.lower().replace('-', '')}"


def risk_assessment_id(patient_id: str, analysis_number: int) -> str:
    return f"ra-{patient_id.lower()}-{analysis_number:03d}"


def build_detected_issue(patient_fhir_ref: str, finding: Finding, identified_at: str) -> dict:
    ev = finding.provenance.evidence[0]
    return {
        "resourceType": "DetectedIssue",
        "id": finding.fhir.detected_issue_id,
        "meta": {"tag": [DATA_ORIGIN_TAG]},
        "status": "final",
        "code": {"text": _ISSUE_CODE[finding.type]},
        "severity": finding.severity.lower(),
        "patient": {"reference": patient_fhir_ref},
        "identifiedDateTime": identified_at,
        "implicated": [{"reference": r} for r in finding.provenance.fhir_resources],
        "detail": f"{finding.rule_id}: {finding.risk}",
        "reference": ev.source_url,
    }


def build_risk_assessment(
    assessment_id: str, patient_fhir_ref: str, level: OverallStatus, basis_refs: list[str],
    occurred_at: str,
) -> dict:
    return {
        "resourceType": "RiskAssessment",
        "id": assessment_id,
        "meta": {"tag": [DATA_ORIGIN_TAG]},
        "status": "final",
        "subject": {"reference": patient_fhir_ref},
        "occurrenceDateTime": occurred_at,
        "basis": [{"reference": r} for r in basis_refs],
        "prediction": [{
            "outcome": {"text": "Medication-related adverse event"},
            "qualitativeRisk": {"coding": [{
                "system": RISK_LEVEL_SYSTEM,
                "code": level.lower().replace("_", "-"),
                "display": _RISK_DISPLAY[level],
            }]},
            "rationale": RISK_RATIONALE,
        }],
        "mitigation": "Prototype output only. No patient-specific treatment recommendation is generated.",
    }
