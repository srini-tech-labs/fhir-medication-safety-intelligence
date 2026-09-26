"""Storage-agnostic clinical repository.

Business logic (snapshot, rule engine, analysis orchestration) depends only on this interface,
never on local files, S3 or any FHIR server.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from app.models.clinical import ClinicalDocument, LabResult, MedicationOrder, PatientRecord


class PatientNotFound(LookupError):
    pass


class DocumentNotFound(LookupError):
    pass


class AnalysisNotFound(LookupError):
    pass


class StoreUnavailable(RuntimeError):
    """A backing store (clinical data or application state) failed. The message is safe for logs; the API maps it to a generic 503."""


class WriteConflict(StoreUnavailable):
    """A conditional write (If-Match) lost to a concurrent change. Subclasses StoreUnavailable, so a caller that
    does not handle it still gets today's generic 503."""


class ClinicalRepository(ABC):
    # ---- source clinical data (read) ------------------------------------------------------
    @abstractmethod
    def get_patients(self) -> list[PatientRecord]: ...

    @abstractmethod
    def get_patient(self, patient_id: str) -> PatientRecord:
        """Raises PatientNotFound."""

    @abstractmethod
    def get_medications(self, patient_id: str) -> list[MedicationOrder]: ...

    @abstractmethod
    def get_observations(self, patient_id: str) -> list[LabResult]: ...

    @abstractmethod
    def get_documents(self, patient_id: str) -> list[ClinicalDocument]: ...

    # ---- derived output (write) -----------------------------------------------------------
    # Called ONLY by AnalysisService.persist_to_fhir() -- an explicit, separate operation the caller must
    # request. analyze()/explain()/run() never call these; normal analysis is read-only by construction.
    @abstractmethod
    def save_detected_issue(self, issue: dict, *, if_match: str | None = None) -> None:
        """Persist a FHIR DetectedIssue produced for one deterministic finding. With ``if_match`` (weak ETag of the
        version the caller read), a versioning store must raise WriteConflict if the resource changed since."""

    @abstractmethod
    def save_risk_assessment(self, assessment: dict) -> None:
        """Persist the qualitative FHIR RiskAssessment roll-up."""

    @abstractmethod
    def get_detected_issue(self, issue_id: str) -> dict | None:
        """Read back a persisted DetectedIssue by id, or None if it does not exist. Used to verify a write."""

    @abstractmethod
    def get_risk_assessment(self, assessment_id: str) -> dict | None:
        """Read back a persisted RiskAssessment by id, or None if it does not exist. Used to verify a write."""

    def write_outputs_enabled(self) -> bool:
        """Whether save_detected_issue/save_risk_assessment actually write anywhere -- a per-backend,
        deployment-level kill switch a cloud backend may implement. Default True: a local/file backend has no
        cost or blast-radius reason to refuse writing its own output files."""
        return True

    @abstractmethod
    def save_analysis(self, patient_id: str, analysis_id: str, analysis: dict) -> None: ...

    @abstractmethod
    def save_rejected_explanation(self, record: dict) -> None:
        """Diagnostic capture of a model completion that was rejected (raw output + category), for human review."""

    @abstractmethod
    def get_analysis(self, patient_id: str, analysis_id: str) -> dict | None:
        """One saved analysis of this patient, or None. Must reject ids that are not this patient's."""

    @abstractmethod
    def get_latest_analysis(self, patient_id: str) -> dict | None: ...

    @abstractmethod
    def next_analysis_number(self, patient_id: str) -> int: ...

    # ---- readiness (cheap reachability only -- never the data itself) --------------------------------------
    def ping(self) -> None:
        """Confirm the CLINICAL data store is reachable. Raises ``StoreUnavailable`` if not.
        Default: always ready -- a local/in-memory backend has no external dependency to check."""

    def ping_app_state(self) -> None:
        """Confirm the APPLICATION STATE store is reachable. Raises ``StoreUnavailable`` if not.
        Default: always ready -- see ``ping()``."""
