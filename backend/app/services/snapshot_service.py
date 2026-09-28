"""Builds the frontend-facing patient views from the repository (no FHIR leaks past this point)."""
from __future__ import annotations

from datetime import date

from app.models.clinical import PatientRecord
from app.models.contract import (
    DocumentContent, PatientInfo, PatientSummary, ReferenceRange, Snapshot, SnapshotDocument,
    SnapshotLab, SnapshotMedication,
)
from app.repository.base import ClinicalRepository, DocumentNotFound


def age_on(birth: date, as_of: date) -> int:
    return as_of.year - birth.year - ((as_of.month, as_of.day) < (birth.month, birth.day))


class SnapshotService:
    def __init__(self, repository: ClinicalRepository, as_of: date):
        self._repo = repository
        self._as_of = as_of

    def _info(self, p: PatientRecord) -> PatientInfo:
        return PatientInfo(id=p.id, name=p.name, age=age_on(p.birth_date, self._as_of), sex=p.sex)

    def list_patients(self) -> list[PatientSummary]:
        return [
            PatientSummary(**self._info(p).model_dump(), scenario_label=p.scenario_label)
            for p in self._repo.get_patients()
        ]

    def snapshot(self, patient_id: str) -> Snapshot:
        patient = self._repo.get_patient(patient_id)
        return Snapshot(
            patient=self._info(patient),
            medications=[
                SnapshotMedication(name=m.name, rx_cui=m.rx_cui, dosage=m.dosage)
                for m in self._repo.get_medications(patient_id)
                if m.status == "active"
            ],
            labs=[
                SnapshotLab(
                    name=l.name, loinc=l.loinc, value=l.value, unit=l.unit,
                    interpretation=l.interpretation, effective_date=l.effective_date.isoformat(),
                    reference_range=(
                        ReferenceRange(low=l.ref_low, high=l.ref_high)
                        if l.ref_low is not None or l.ref_high is not None else None
                    ),
                )
                for l in self._repo.get_observations(patient_id)
            ],
            documents=[
                SnapshotDocument(id=d.id, type=d.type, title=d.title,
                                 date=d.date.isoformat() if d.date else None)
                for d in self._repo.get_documents(patient_id)
            ],
        )

    def document(self, patient_id: str, document_id: str) -> DocumentContent:
        self._repo.get_patient(patient_id)
        for d in self._repo.get_documents(patient_id):
            if d.id == document_id:
                return DocumentContent(id=d.id, type=d.type, title=d.title, text=d.text,
                                       date=d.date.isoformat() if d.date else None)
        raise DocumentNotFound(document_id)
