"""ClinicalRepository backed by AWS HealthLake's FHIR REST API (the cloud FHIR system of record).

Reads: one HealthLake search per patient (`Patient?identifier=<system>|Pxxx` with `_revinclude` of the patient's
MedicationRequest / Observation / Encounter / DocumentReference) plus `Binary/{id}` for notes. The resources are assembled into a
Bundle dict and handed to the SAME `parse_bundle()` the local repository uses, so the rule engine sees identical domain objects
regardless of backend. Nothing is indexed or searched locally.

Writes: `save_detected_issue` / `save_risk_assessment` PUT the generated FHIR resources (deterministic ids => idempotent upsert,
history preserved) when `write_fhir_outputs` is enabled; otherwise they are no-ops. Called only by
`AnalysisService.persist_to_fhir()`, an explicit operation separate from `analyze()` -- normal analysis never
writes here. `get_detected_issue` / `get_risk_assessment` read a persisted resource back by id, used by
`persist_to_fhir()` to verify what it just wrote.
Application state (analysis JSON, rejected-output captures) is not FHIR data and stays in `AppStateStore`.
"""
from __future__ import annotations

import functools
import time
from typing import Callable

from app.models.clinical import ClinicalDocument, LabResult, MedicationOrder, PatientRecord
from app.repository.app_state import AppStateStore
from app.repository.base import ClinicalRepository, PatientNotFound, StoreUnavailable, WriteConflict
from app.repository.fhir_parser import ParsedBundle, parse_bundle
from app.repository.healthlake_client import (
    HealthLakeClient, HealthLakeError, HealthLakeNotFound, HealthLakeValidationError,
)
from app.terminology import Terminology

IDENTIFIER_SYSTEM = "https://example.org/synthetic-patient-id"
PATIENT_LINKED_TYPES = ("MedicationRequest", "Observation", "Encounter", "DocumentReference")
_TYPE_ORDER = {t: i for i, t in enumerate(("Patient", "Encounter", "MedicationRequest", "Observation", "DocumentReference", "Binary"))}


def _store_errors(fn):
    """Any HealthLake failure becomes the backend-agnostic ``StoreUnavailable`` (the API answers a generic 503; cause stays in logs)."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except HealthLakeError as exc:
            raise StoreUnavailable(f"clinical data store failed: {type(exc).__name__} (HTTP {exc.status})") from exc
    return wrapper


def _sorted(resources: list[dict]) -> list[dict]:
    """Stable order = resource type then FHIR id, which equals the local bundle order for the frozen data."""
    return sorted(resources, key=lambda r: (_TYPE_ORDER.get(r["resourceType"], 99), r.get("id", "")))


class HealthLakeFHIRRepository(ClinicalRepository):
    def __init__(self, client: HealthLakeClient, terminology: Terminology, app_state: AppStateStore, *,
                 identifier_system: str = IDENTIFIER_SYSTEM, cache_ttl: float = 60.0,
                 clock: Callable[[], float] = time.monotonic, write_fhir_outputs: bool = False):
        self._client, self._terms, self._state = client, terminology, app_state
        self._write_outputs = write_fhir_outputs
        self._system, self._ttl, self._clock = identifier_system, cache_ttl, clock
        self._cache: dict[str, tuple[float, ParsedBundle]] = {}

    @classmethod
    def from_settings(cls, settings) -> "HealthLakeFHIRRepository":
        client = HealthLakeClient(settings.healthlake_datastore_id, region=settings.healthlake_region,
                                  endpoint=settings.healthlake_endpoint, role_arn=settings.healthlake_role_arn)
        if settings.app_state_backend == "dynamodb":
            from app.repository.app_state_dynamodb import DynamoAppStateStore

            state = DynamoAppStateStore(settings.app_state_table, region=settings.healthlake_region)
        else:
            state = AppStateStore(settings.output_dir)
        return cls(client, Terminology.load(settings.package_dir), state, write_fhir_outputs=settings.healthlake_write_outputs)

    # ---- reading source data --------------------------------------------------------------------------
    def _patient_bundle(self, app_id: str) -> list[dict]:
        """All resources of one patient, via HealthLake search (falls back from `patient` to `subject` spellings, then per type)."""
        ident = f"{self._system}|{app_id}"
        for param in ("patient", "subject"):
            params = [("identifier", ident), ("_count", "100"), *[("_revinclude", f"{t}:{param}") for t in PATIENT_LINKED_TYPES]]
            try:
                resources = [e["resource"] for e in self._client.search_entries("Patient", params)]
                break
            except HealthLakeValidationError:
                continue
        else:
            return self._per_type(ident)
        return resources

    def _per_type(self, ident: str) -> list[dict]:
        patients = [e["resource"] for e in self._client.search_entries("Patient", [("identifier", ident)])]
        out = list(patients)
        for p in patients:
            for t in PATIENT_LINKED_TYPES:
                out += [e["resource"] for e in self._client.search_entries(t, [("patient", f"Patient/{p['id']}"), ("_count", "100")])]
        return out

    @_store_errors
    def _load(self, app_id: str) -> ParsedBundle:
        hit = self._cache.get(app_id)
        if hit and self._clock() - hit[0] < self._ttl:
            return hit[1]
        resources = self._patient_bundle(app_id)
        patients = [r for r in resources if r["resourceType"] == "Patient"]
        if not patients:
            raise PatientNotFound(app_id)
        if len(patients) > 1:
            raise HealthLakeError(f"identifier {app_id!r} matched {len(patients)} Patient resources")
        patient_id = patients[0]["id"]
        linked = [r for r in resources if r["resourceType"] != "Patient"
                  and f"Patient/{patient_id}" in _subject_refs(r)]
        for doc in [r for r in linked if r["resourceType"] == "DocumentReference"]:
            for content in doc.get("content", []):
                url = content.get("attachment", {}).get("url", "")
                if url.startswith("Binary/"):
                    try:
                        linked.append(self._client.read("Binary", url.split("/", 1)[1]))
                    except HealthLakeNotFound:
                        pass  # parse_bundle skips a DocumentReference whose Binary is missing
        bundle = {"resourceType": "Bundle", "type": "collection",
                  "entry": [{"resource": r} for r in _sorted([patients[0], *linked])]}
        parsed = parse_bundle(bundle, self._terms)
        self._cache[app_id] = (self._clock(), parsed)
        return parsed

    @_store_errors
    def get_patients(self) -> list[PatientRecord]:
        entries = list(self._client.search_entries("Patient", [("_count", "100"), ("_revinclude", "Encounter:patient")]))
        resources = [e["resource"] for e in entries]
        encounters = [r for r in resources if r["resourceType"] == "Encounter"]
        records = []
        for p in (r for r in resources if r["resourceType"] == "Patient"):
            if not any(i.get("system") == self._system for i in p.get("identifier", [])):
                continue
            mine = [e for e in encounters if f"Patient/{p['id']}" in _subject_refs(e)]
            records.append(parse_bundle({"resourceType": "Bundle", "type": "collection",
                                         "entry": [{"resource": r} for r in _sorted([p, *mine])]}, self._terms).patient)
        return sorted(records, key=lambda r: r.id)

    def get_patient(self, patient_id: str) -> PatientRecord:
        return self._load(patient_id).patient

    def get_medications(self, patient_id: str) -> list[MedicationOrder]:
        return list(self._load(patient_id).medications)

    def get_observations(self, patient_id: str) -> list[LabResult]:
        return list(self._load(patient_id).labs)

    def get_documents(self, patient_id: str) -> list[ClinicalDocument]:
        return list(self._load(patient_id).documents)

    # ---- writing generated FHIR --------------------------------------------------------------------------
    # Off by default. The deployment-level kill switch: even with an explicit persist request (the only caller
    # -- see AnalysisService.persist_to_fhir()), nothing is written to HealthLake unless an operator has set
    # HEALTHLAKE_WRITE_OUTPUTS=true. Least-privilege IAM (healthlake:UpdateResource only -- FHIR PUT is the
    # "update" interaction and creates the initial version itself when the id doesn't yet exist, so
    # CreateResource is deliberately not granted unless a live PUT actually demonstrates it's required) is the
    # second, independent gate -- see docs/adr and the Phase 6 write-back deployment plan for both.
    @_store_errors
    def save_detected_issue(self, issue: dict, *, if_match: str | None = None) -> None:
        if self._write_outputs:
            resp = self._client.put(issue, if_match=if_match)
            # Raised before raise_for_status(): WriteConflict is not a HealthLakeError, so _store_errors lets it
            # through unchanged for the caller's re-read/re-merge retry.
            if if_match and resp.status == 412:
                raise WriteConflict(f"DetectedIssue/{issue['id']} changed since it was read")
            resp.raise_for_status()

    @_store_errors
    def save_risk_assessment(self, assessment: dict) -> None:
        if self._write_outputs:
            self._client.put(assessment).raise_for_status()

    @_store_errors
    def get_detected_issue(self, issue_id: str) -> dict | None:
        try:
            return self._client.read("DetectedIssue", issue_id)
        except HealthLakeNotFound:
            return None

    @_store_errors
    def get_risk_assessment(self, assessment_id: str) -> dict | None:
        try:
            return self._client.read("RiskAssessment", assessment_id)
        except HealthLakeNotFound:
            return None

    def write_outputs_enabled(self) -> bool:
        return self._write_outputs

    # ---- application state (not FHIR data) ----------------------------------------------------------------
    def save_analysis(self, patient_id: str, analysis_id: str, analysis: dict) -> None:
        self._state.save_analysis(patient_id, analysis_id, analysis)

    def get_analysis(self, patient_id: str, analysis_id: str) -> dict | None:
        self.get_patient(patient_id)  # unknown patients raise PatientNotFound
        return self._state.get_analysis(patient_id, analysis_id)

    def get_latest_analysis(self, patient_id: str) -> dict | None:
        self.get_patient(patient_id)
        return self._state.get_latest_analysis(patient_id)

    def next_analysis_number(self, patient_id: str) -> int:
        return self._state.next_analysis_number(patient_id)

    def save_rejected_explanation(self, record: dict) -> None:
        self._state.save_rejected_explanation(record)

    # ---- readiness --------------------------------------------------------------------------------------
    @_store_errors
    def ping(self) -> None:
        """Capability statement only: proves auth (SigV4) and connectivity, reads no PHI, matches V0."""
        self._client.request("GET", "metadata").raise_for_status()

    def ping_app_state(self) -> None:
        self._state.ping()  # local AppStateStore or DynamoAppStateStore -- both duck-type this method


def _subject_refs(resource: dict) -> set[str]:
    refs = set()
    for key in ("subject", "patient"):
        ref = (resource.get(key) or {}).get("reference")
        if ref:
            refs.add(ref)
    return refs
