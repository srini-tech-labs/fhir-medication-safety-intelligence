"""Internal clinical domain objects produced by the repository layer.

These carry FHIR references (for provenance) but are never sent to the frontend directly;
the snapshot/analysis services map them onto the API contract in ``contract.py``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class PatientRecord:
    id: str  # application id, e.g. "P001"
    fhir_ref: str  # e.g. "Patient/patient-p001"
    name: str
    sex: str
    birth_date: date
    scenario_label: str | None


@dataclass(frozen=True)
class MedicationOrder:
    fhir_ref: str  # e.g. "MedicationRequest/medreq-p001-01"
    name: str
    rx_cui: str | None
    dosage: str | None
    status: str


@dataclass(frozen=True)
class LabResult:
    fhir_ref: str  # e.g. "Observation/obs-p001-01"
    name: str
    loinc: str | None
    value: float
    unit: str | None
    interpretation: str | None
    effective_date: date
    ref_low: float | None = None
    ref_high: float | None = None


@dataclass(frozen=True)
class ClinicalDocument:
    id: str  # contract id, e.g. "DOC-P008-01"
    fhir_ref: str  # e.g. "DocumentReference/docref-p008-note"
    type: str
    title: str | None
    date: date | None
    text: str
