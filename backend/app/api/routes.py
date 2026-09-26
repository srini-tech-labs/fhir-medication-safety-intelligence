"""Stable REST contract (data/phase0_v1_0/api/openapi.yaml). Returns application JSON, never FHIR."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from app.api.deps import get_container
from app.container import Container
from app.models.contract import AIExplanation, Analysis, DocumentContent, FhirPersistResult, PatientSummary, Snapshot
from app.redact import redact
from app.repository.base import AnalysisNotFound, DocumentNotFound, PatientNotFound, StoreUnavailable
from app.services.analysis_service import AnalysisNotPersistable, FhirPersistVerificationFailed, FhirWriteDisabled
from app.services.explanation.factory import SafeExplanationService
from app.services.explanation.failures import UNAVAILABLE, public_reason

log = logging.getLogger(__name__)
router = APIRouter(prefix="/v1")


@router.get("/health")
def health() -> dict:
    """Liveness: is the process up and able to answer at all? Always ``ok`` once the app has started -- checks
    nothing external, so it can never be dragged down by a backing dependency (that is what ``/ready`` is for).
    An orchestrator should restart the instance if THIS stops answering; it must not restart it just because a
    backing store is briefly unreachable -- ``/ready`` reports that separately."""
    return {"status": "ok"}


@router.get("/ready")
def ready(c: Container = Depends(get_container)) -> JSONResponse:
    """Readiness: can this instance usefully serve traffic right now? Separate from liveness (``/health``) on
    purpose -- a backing-store outage should take an instance out of a load balancer's rotation without an
    orchestrator concluding the process itself is broken and restarting it.

    Checks are cheap, read-only, and reveal only a per-dependency ``ok``/``unavailable`` label -- never a
    resource identifier, hostname, exception message or stack trace (those stay in the redacted server log,
    same as the rest of the API). None of them call the AI explanation provider: that would spend a model call
    on every poll, and its outage does not need to fail readiness anyway, because the explanation endpoint
    already has a tested, labelled fallback (deterministic-only, publicly coded ``EXPLANATION_UNAVAILABLE``) --
    the core, deterministic path keeps working without it. So ``explanationProvider`` is reported (``configured``
    = a real provider is wired, independent of whether it can currently be invoked) but never lowers the overall
    status; only ``clinicalStore`` and ``appState`` are hard dependencies with no fallback, and either being
    unreachable already means most other routes answer 503.
    """
    checks: dict[str, str] = {}
    for name, probe in (("clinicalStore", c.repository.ping), ("appState", c.repository.ping_app_state)):
        try:
            probe()
            checks[name] = "ok"
        except StoreUnavailable:
            checks[name] = "unavailable"
    checks["explanationProvider"] = "configured" if isinstance(c.explainer, SafeExplanationService) else "not_configured"
    is_ready = checks["clinicalStore"] == "ok" and checks["appState"] == "ok"
    return JSONResponse(status_code=200 if is_ready else 503,
                        content={"status": "ready" if is_ready else "not_ready", "checks": checks})


@router.get("/patients")
def list_patients(c: Container = Depends(get_container)) -> dict[str, list[PatientSummary]]:
    return {"patients": c.snapshots.list_patients()}


@router.get("/patients/{patient_id}/snapshot")
def get_snapshot(patient_id: str, c: Container = Depends(get_container)) -> Snapshot:
    try:
        return c.snapshots.snapshot(patient_id)
    except PatientNotFound:
        raise HTTPException(404, f"Patient {patient_id} not found")


@router.post("/patients/{patient_id}/analyses")
def run_analysis(patient_id: str, c: Container = Depends(get_container)) -> Analysis:
    """Deterministic analysis only: returns as soon as the rules have run (``aiExplanation`` is null)."""
    try:
        return c.analyses.analyze(patient_id)
    except PatientNotFound:
        raise HTTPException(404, f"Patient {patient_id} not found")


@router.post("/patients/{patient_id}/analyses/{analysis_id}/explanation")
def explain_analysis(patient_id: str, analysis_id: str, c: Container = Depends(get_container)) -> AIExplanation:
    """AI explanation of a saved analysis, requested separately. A model failure yields a labelled mock explanation
    (HTTP 200, public ``fallbackCode``); an unexpected error yields a generic 503. Neither touches the findings."""
    try:
        return c.analyses.explain(patient_id, analysis_id)
    except (PatientNotFound, AnalysisNotFound):
        raise HTTPException(404, f"Analysis {analysis_id} not found for patient {patient_id}")
    except Exception as exc:  # noqa: BLE001
        log.warning("explanation request failed for %s: %s", analysis_id, redact(f"{type(exc).__name__}: {exc}")[:300])
        raise HTTPException(503, public_reason(UNAVAILABLE))


@router.post("/patients/{patient_id}/analyses/{analysis_id}/persist")
def persist_analysis(patient_id: str, analysis_id: str, c: Container = Depends(get_container)) -> FhirPersistResult:
    """Explicit, separate write-back: persists ONLY the deterministic findings/risk assessment of a saved
    analysis to the clinical FHIR store (DetectedIssue / RiskAssessment). Never called by the analyze or explain
    routes -- normal analysis stays read-only. AI explanation text is never read or persisted here. Every
    written resource is read back and verified before this returns 200; a verification mismatch is reported as
    503, never a partial success. 409 when the repository's deployment-level write kill switch is off, or when
    the saved analysis's status is not COMPLETED."""
    try:
        return c.analyses.persist_to_fhir(patient_id, analysis_id)
    except (PatientNotFound, AnalysisNotFound):
        raise HTTPException(404, f"Analysis {analysis_id} not found for patient {patient_id}")
    except FhirWriteDisabled:
        raise HTTPException(409, "FHIR write-back to the clinical data store is disabled in this deployment")
    except AnalysisNotPersistable:
        raise HTTPException(409, f"Analysis {analysis_id} is not in a persistable state")
    except (StoreUnavailable, FhirPersistVerificationFailed) as exc:
        log.warning("persist-to-FHIR failed for %s: %s", analysis_id, redact(f"{type(exc).__name__}: {exc}")[:300])
        raise HTTPException(503, "Could not persist to the clinical data store")


@router.get("/patients/{patient_id}/analyses/latest")
def latest_analysis(patient_id: str, c: Container = Depends(get_container)) -> Analysis:
    try:
        analysis = c.analyses.latest(patient_id)
    except PatientNotFound:
        raise HTTPException(404, f"Patient {patient_id} not found")
    if analysis is None:
        raise HTTPException(404, f"No analysis has been run for patient {patient_id}")
    return analysis


@router.get("/patients/{patient_id}/documents/{document_id}")
def get_document(patient_id: str, document_id: str, c: Container = Depends(get_container)) -> DocumentContent:
    try:
        return c.snapshots.document(patient_id, document_id)
    except PatientNotFound:
        raise HTTPException(404, f"Patient {patient_id} not found")
    except DocumentNotFound:
        raise HTTPException(404, f"Document {document_id} not found")
