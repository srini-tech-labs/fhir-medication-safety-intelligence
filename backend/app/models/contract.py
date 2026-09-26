"""Application JSON contract (what the frontend sees). Mirrors data/phase0_v1_0/api/openapi.yaml.

Fields beyond the frozen examples (provenance, asOfDate, ...) are strictly additive.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

Severity = Literal["HIGH", "MODERATE", "LOW"]
OverallStatus = Literal["HIGH", "MODERATE", "LOW", "NONE", "NEEDS_DATA"]


class CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


# ---- patients / snapshot -------------------------------------------------------------------
class PatientSummary(CamelModel):
    id: str
    name: str
    age: int
    sex: str
    scenario_label: str | None = None


class PatientInfo(CamelModel):
    id: str
    name: str
    age: int
    sex: str


class SnapshotMedication(CamelModel):
    name: str
    rx_cui: str | None
    dosage: str | None


class ReferenceRange(CamelModel):
    low: float | None = None
    high: float | None = None


class SnapshotLab(CamelModel):
    name: str
    loinc: str | None
    value: float
    unit: str | None
    interpretation: str | None
    effective_date: str
    reference_range: ReferenceRange | None = None


class SnapshotDocument(CamelModel):
    id: str
    type: str
    date: str | None
    title: str | None = None


class Snapshot(CamelModel):
    patient: PatientInfo
    medications: list[SnapshotMedication]
    labs: list[SnapshotLab]
    documents: list[SnapshotDocument]


class DocumentContent(CamelModel):
    id: str
    type: str
    date: str | None
    title: str | None = None
    text: str


# ---- evidence / provenance -----------------------------------------------------------------
class EvidenceReference(CamelModel):
    """One entry of the curated evidence catalog (rules/rule_evidence.json)."""

    evidence_id: str
    title: str
    source: str
    section: str
    source_url: str
    summary: str
    accessed: str
    source_type: str  # FDA_DRUG_LABEL | CLINICAL_GUIDELINE
    provider: str  # DailyMed | American Diabetes Association


class FindingSource(CamelModel):
    type: str
    provider: str
    evidence_ids: list[str]


class Trigger(CamelModel):
    operator: str
    threshold: float
    unit: str
    note: str | None = None


class Provenance(CamelModel):
    rule_version: str
    trigger: Trigger | None = None
    fhir_resources: list[str]
    evidence: list[EvidenceReference]
    severity_disclaimer: str | None = None


class EvidenceMedication(CamelModel):
    name: str
    rx_cui: str
    dosage: str | None = None


class EvidenceLab(CamelModel):
    name: str
    loinc: str
    value: float
    unit: str
    interpretation: str | None = None
    effective_date: str | None = None


class FindingEvidence(CamelModel):
    medications: list[EvidenceMedication]
    labs: list[EvidenceLab]


class FhirLink(CamelModel):
    detected_issue_id: str


# ---- findings / gaps -----------------------------------------------------------------------
class Finding(CamelModel):
    finding_id: str
    rule_id: str
    type: Literal["DRUG_DRUG", "DRUG_LAB"]
    severity: Severity
    title: str
    evidence: FindingEvidence
    risk: str
    source: FindingSource
    provenance: Provenance
    fhir: FhirLink | None = None


class RequiredLab(CamelModel):
    name: str
    loinc: str


class DataGap(CamelModel):
    gap_id: str
    rule_id: str
    type: Literal["DATA_GAP"] = "DATA_GAP"
    status: Literal["NEEDS_DATA"] = "NEEDS_DATA"
    title: str
    finding: str
    medication: EvidenceMedication
    required_lab: RequiredLab
    lookback_days: int
    source: FindingSource
    provenance: Provenance


class AnalysisSummary(CamelModel):
    total_findings: int
    high: int
    moderate: int
    low: int
    data_gaps: int


class RiskAssessmentSummary(CamelModel):
    id: str
    level: OverallStatus
    rationale: str
    basis: list[str]


# ---- AI explanation ------------------------------------------------------------------------
class FindingExplanation(CamelModel):
    rule_id: str
    explanation: str


class AIExplanation(CamelModel):
    text: str
    summary: str
    finding_explanations: list[FindingExplanation]
    data_gap_explanation: str | None
    grounded_in_findings_only: bool
    mode: Literal["mock", "llm"]
    model: str | None = None
    fallback_code: str | None = None  # stable public category (see services/explanation/failures.py)
    fallback_reason: str | None = None  # public message for that category; never provider detail


class FhirPersistResult(CamelModel):
    """Response of the explicit, separate persist operation (AnalysisService.persist_to_fhir). Built only from
    deterministic findings/risk assessment; never touches ai_explanation."""
    status: Literal["PERSISTED"]
    detected_issue_ids: list[str]
    risk_assessment_id: str | None


class FhirPersistenceReceipt(CamelModel):
    """Recorded on the analysis record when persist_to_fhir() succeeds, so a later GET .../analyses/latest can
    show "already saved" without re-persisting. A small receipt only -- never a copy of the DetectedIssue/
    RiskAssessment resources themselves; the clinical FHIR store remains the sole source of truth for those."""
    status: Literal["PERSISTED"]
    detected_issue_ids: list[str]
    risk_assessment_id: str | None
    persisted_at: str


class Analysis(CamelModel):
    analysis_id: str
    patient_id: str
    status: Literal["COMPLETED", "FAILED"]
    overall_severity: OverallStatus
    summary: AnalysisSummary
    findings: list[Finding]
    data_gaps: list[DataGap]
    risk_assessment: RiskAssessmentSummary | None = None
    ai_explanation: AIExplanation | None = None
    fhir_persistence: FhirPersistenceReceipt | None = None
    as_of_date: str
    generated_at: str
    rules_version: str
