"""ClinicalRepository over per-patient FHIR R4 Bundles.

All FHIR handling and output-key layout lives here; subclasses supply only four storage
primitives (list/read bundles, write/read/list derived JSON). ``LocalFHIRRepository`` uses the
filesystem. The cloud backend is a separate ``ClinicalRepository`` implementation (see ``factory.py``).
"""
from __future__ import annotations

import re
from abc import abstractmethod

from app.models.clinical import ClinicalDocument, LabResult, MedicationOrder, PatientRecord
from app.repository.base import ClinicalRepository, PatientNotFound
from app.repository.fhir_parser import ParsedBundle, parse_bundle
from app.terminology import Terminology


class FHIRBundleRepository(ClinicalRepository):
    def __init__(self, terminology: Terminology):
        self._terms = terminology
        self._cache: dict[str, ParsedBundle] = {}

    # ---- storage primitives ---------------------------------------------------------------
    @abstractmethod
    def _bundle_patient_ids(self) -> list[str]: ...

    @abstractmethod
    def _read_bundle(self, patient_id: str) -> dict: ...

    @abstractmethod
    def _write_output(self, key: str, document: dict) -> None: ...

    @abstractmethod
    def _read_output(self, key: str) -> dict | None: ...

    @abstractmethod
    def _list_output_keys(self, prefix: str) -> list[str]: ...

    # ---- source data ----------------------------------------------------------------------
    def _bundle(self, patient_id: str) -> ParsedBundle:
        if patient_id not in self._bundle_patient_ids():
            raise PatientNotFound(patient_id)
        if patient_id not in self._cache:
            self._cache[patient_id] = parse_bundle(self._read_bundle(patient_id), self._terms)
        return self._cache[patient_id]

    def get_patients(self) -> list[PatientRecord]:
        return [self._bundle(pid).patient for pid in sorted(self._bundle_patient_ids())]

    def get_patient(self, patient_id: str) -> PatientRecord:
        return self._bundle(patient_id).patient

    def get_medications(self, patient_id: str) -> list[MedicationOrder]:
        return list(self._bundle(patient_id).medications)

    def get_observations(self, patient_id: str) -> list[LabResult]:
        return list(self._bundle(patient_id).labs)

    def get_documents(self, patient_id: str) -> list[ClinicalDocument]:
        return list(self._bundle(patient_id).documents)

    # ---- derived output -------------------------------------------------------------------
    def save_detected_issue(self, issue: dict, *, if_match: str | None = None) -> None:  # local files: unversioned
        self._write_output(f"detected_issues/{issue['id']}.json", issue)

    def save_risk_assessment(self, assessment: dict) -> None:
        self._write_output(f"risk_assessments/{assessment['id']}.json", assessment)

    def get_detected_issue(self, issue_id: str) -> dict | None:
        return self._read_output(f"detected_issues/{issue_id}.json")

    def get_risk_assessment(self, assessment_id: str) -> dict | None:
        return self._read_output(f"risk_assessments/{assessment_id}.json")

    def save_analysis(self, patient_id: str, analysis_id: str, analysis: dict) -> None:
        self._write_output(f"analyses/{patient_id}/{analysis_id}.json", analysis)

    def save_rejected_explanation(self, record: dict) -> None:
        stamp = re.sub(r"[^0-9A-Za-z]", "", record["capturedAt"])
        self._write_output(f"rejected_explanations/{record['analysisId']}-{stamp}.json", record)

    def _analysis_keys(self, patient_id: str) -> list[str]:
        return sorted(self._list_output_keys(f"analyses/{patient_id}/"))

    def get_analysis(self, patient_id: str, analysis_id: str) -> dict | None:
        self._bundle(patient_id)  # unknown patients raise PatientNotFound
        # The id becomes part of a storage key, so accept only this patient's own id shape (no path tricks).
        if not re.fullmatch(rf"AN-{re.escape(patient_id)}-\d{{3,}}", analysis_id):
            return None
        return self._read_output(f"analyses/{patient_id}/{analysis_id}.json")

    def get_latest_analysis(self, patient_id: str) -> dict | None:
        self._bundle(patient_id)  # 404 for unknown patients
        keys = self._analysis_keys(patient_id)
        return self._read_output(keys[-1]) if keys else None

    def next_analysis_number(self, patient_id: str) -> int:
        return len(self._analysis_keys(patient_id)) + 1
