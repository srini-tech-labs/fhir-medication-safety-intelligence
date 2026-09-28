"""Orchestrates one medication-safety analysis (handoff section 15).

``analyze``        steps 1-9, purely deterministic: load meds/labs -> normalize -> DDI -> drug-lab -> data gaps ->
                    overall -> findings -> qualitative risk roll-up. Persisted immediately (application state
                    only); never calls an AI service, never writes a FHIR resource to the clinical store.
``explain``         step 10, separate and later: reads the *saved* analysis and asks the explanation service to
                    describe it. It can only add ``aiExplanation`` to the stored record; it cannot change findings.
``persist_to_fhir`` a THIRD, separate and explicit step, never called by the other three: builds DetectedIssue /
                    RiskAssessment from the *saved* analysis's deterministic fields only (``ai_explanation`` is
                    never read here, so it cannot affect what gets written) and PUTs them to the clinical store,
                    then reads each one back to verify the write actually took, before reporting success. Records
                    a small receipt (ids + timestamp only, never a resource copy) on the analysis record so a
                    later read of the saved analysis can show "already saved" without re-persisting.
``run``             synchronous convenience (analyze then a best-effort explain) for scripts, tests and the live
                    harness. Never calls persist_to_fhir -- that remains an explicit, separate operator action.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone

from app.models.contract import (
    AIExplanation,
    Analysis,
    FhirLink,
    FhirPersistenceReceipt,
    FhirPersistResult,
    RiskAssessmentSummary,
)
from app.redact import redact
from app.repository.base import AnalysisNotFound, ClinicalRepository, WriteConflict
from app.rules.catalog import RuleCatalog
from app.rules.engine import RuleEngine
from app.services import fhir_outputs
from app.services.explanation.base import ExplanationService
from app.services.snapshot_service import SnapshotService

log = logging.getLogger(__name__)


class FhirWriteDisabled(RuntimeError):
    """persist_to_fhir() was called but the repository's deployment-level write kill switch is off
    (write_outputs_enabled() returned False)."""


class FhirPersistVerificationFailed(RuntimeError):
    """A resource could not be read back after writing it, or its content did not match what was sent. Treated
    as a hard failure -- persist_to_fhir() never reports success for a write it could not independently confirm."""


class AnalysisNotPersistable(RuntimeError):
    """persist_to_fhir() was called for an analysis whose stored status is not COMPLETED (e.g. FAILED). The
    contract allows a FAILED analysis to be saved; this refuses to build/write FHIR resources from one."""


class AnalysisService:
    def __init__(
        self,
        repository: ClinicalRepository,
        catalog: RuleCatalog,
        explainer: ExplanationService,
        snapshots: SnapshotService,
        as_of: date,
    ):
        self._repo = repository
        self._catalog = catalog
        self._engine = RuleEngine(catalog)
        self._explainer = explainer
        self._snapshots = snapshots
        self._as_of = as_of

    def analyze(self, patient_id: str) -> Analysis:
        """Deterministic analysis only (no AI, no wait). ``aiExplanation`` is null until ``explain`` is called."""
        patient = self._repo.get_patient(patient_id)  # raises PatientNotFound
        medications = self._repo.get_medications(patient_id)
        labs = self._repo.get_observations(patient_id)

        result = self._engine.evaluate(patient_id, medications, labs, self._as_of)
        number = self._repo.next_analysis_number(patient_id)
        analysis_id = f"AN-{patient_id}-{number:03d}"
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")

        findings = [
            f.model_copy(update={"fhir": FhirLink(
                detected_issue_id=fhir_outputs.detected_issue_id(patient_id, f.rule_id))})
            for f in result.findings
        ]
        basis = [m.fhir_ref for m in medications if m.status == "active"] + [l.fhir_ref for l in labs]
        risk = RiskAssessmentSummary(
            id=fhir_outputs.risk_assessment_id(patient_id, number),
            level=result.overall,
            rationale=fhir_outputs.RISK_RATIONALE,
            basis=basis,
        )
        analysis = Analysis(
            analysis_id=analysis_id, patient_id=patient_id, status="COMPLETED",
            overall_severity=result.overall, summary=result.summary, findings=findings,
            data_gaps=result.data_gaps, risk_assessment=risk, ai_explanation=None,
            as_of_date=self._as_of.isoformat(), generated_at=now, rules_version=self._catalog.version,
        )
        self._repo.save_analysis(patient_id, analysis_id, analysis.model_dump(mode="json", by_alias=True))
        return analysis

    def explain(self, patient_id: str, analysis_id: str) -> AIExplanation:
        """Explanation for a *saved* analysis. Raises PatientNotFound / AnalysisNotFound.

        Idempotent: a stored successful explanation is returned as-is (no repeat model call). A stored fallback is
        regenerated, so a retry after a transient failure works. Other exceptions from the explainer propagate to the
        caller; the deterministic record is never modified beyond adding ``aiExplanation``.
        """
        stored = self._repo.get_analysis(patient_id, analysis_id)
        if stored is None:
            raise AnalysisNotFound(analysis_id)
        analysis = Analysis.model_validate(stored)
        if analysis.ai_explanation is not None and analysis.ai_explanation.fallback_code is None:
            return analysis.ai_explanation

        documents = self._repo.get_documents(patient_id)
        note_context = "\n\n".join(f"{d.type}: {d.text}" for d in documents) or None
        # The explainer gets its own deep copy: nothing it does to its input can reach the record we save below.
        deterministic_only = analysis.model_copy(update={"ai_explanation": None}, deep=True)
        explanation = self._explainer.explain(self._snapshots.snapshot(patient_id), deterministic_only, note_context)

        updated = analysis.model_copy(update={"ai_explanation": explanation})
        self._repo.save_analysis(patient_id, analysis_id, updated.model_dump(mode="json", by_alias=True))
        return explanation

    def persist_to_fhir(self, patient_id: str, analysis_id: str) -> FhirPersistResult:
        """Explicit write-back: persist ONLY the deterministic outputs of an already-saved analysis to the
        clinical FHIR store (DetectedIssue per finding, one RiskAssessment roll-up). Never called by
        analyze()/explain()/run() -- a caller must request this separately, e.g. a "Save findings to the clinical
        store" UI action. Reads the *saved* analysis (never re-runs the rules) and reads ``ai_explanation`` from nowhere
        at all in this method, so AI text cannot affect what gets built or written, even if present on the
        stored record. Raises PatientNotFound / AnalysisNotFound for an unknown id or one that does not belong
        to patient_id (get_analysis rejects any id shape not scoped to this exact patient), AnalysisNotPersistable
        if the stored analysis's status is not COMPLETED, FhirWriteDisabled if the repository's deployment-level
        write kill switch is off, FhirPersistVerificationFailed if a resource cannot be read back matching what
        was sent, or StoreUnavailable for any other store failure (e.g. IAM denies the write) -- never a partial
        or unverified success.
        """
        if not self._repo.write_outputs_enabled():
            raise FhirWriteDisabled("FHIR write-back is disabled for this deployment")
        stored = self._repo.get_analysis(patient_id, analysis_id)
        if stored is None:
            raise AnalysisNotFound(analysis_id)
        analysis = Analysis.model_validate(stored)
        if analysis.status != "COMPLETED":
            raise AnalysisNotPersistable(f"analysis {analysis_id} has status {analysis.status!r}, not COMPLETED")
        patient = self._repo.get_patient(patient_id)  # raises PatientNotFound
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")

        detected_issue_ids: list[str] = []
        for f in analysis.findings:
            issue = fhir_outputs.build_detected_issue(patient.fhir_ref, f, now)
            self._save_detected_issue_preserving_provider_fields(issue)
            # verifies producer-owned content only; provider-owned mitigation may legitimately change afterwards
            self._verify_persisted("DetectedIssue", issue, self._repo.get_detected_issue)
            detected_issue_ids.append(issue["id"])

        risk_assessment_id: str | None = None
        if analysis.risk_assessment is not None:
            ra = analysis.risk_assessment
            assessment = fhir_outputs.build_risk_assessment(ra.id, patient.fhir_ref, ra.level, ra.basis, now)
            self._repo.save_risk_assessment(assessment)
            self._verify_persisted("RiskAssessment", assessment, self._repo.get_risk_assessment)
            risk_assessment_id = assessment["id"]

        # Record a small receipt on the analysis record -- ids and a timestamp only, never a copy of the FHIR
        # resources themselves -- so a later GET .../analyses/latest can show "already saved" without
        # re-persisting. Reuses the same save_analysis() path analyze()/explain() already use; no new store.
        receipt = FhirPersistenceReceipt(status="PERSISTED", detected_issue_ids=detected_issue_ids,
                                          risk_assessment_id=risk_assessment_id, persisted_at=now)
        updated = analysis.model_copy(update={"fhir_persistence": receipt})
        self._repo.save_analysis(patient_id, analysis_id, updated.model_dump(mode="json", by_alias=True))

        return FhirPersistResult(status="PERSISTED", detected_issue_ids=detected_issue_ids,
                                 risk_assessment_id=risk_assessment_id)

    _MAX_DETECTED_ISSUE_WRITE_ATTEMPTS = 3

    def _save_detected_issue_preserving_provider_fields(self, issue: dict) -> None:
        """Read-merge-write, conditional on the version just read (If-Match); a concurrent change is re-read
        and re-merged, never blindly overwritten."""
        for attempt in range(self._MAX_DETECTED_ISSUE_WRITE_ATTEMPTS):
            current = self._repo.get_detected_issue(issue["id"])
            merged = fhir_outputs.preserve_provider_fields(issue, current)
            try:
                self._repo.save_detected_issue(merged, if_match=fhir_outputs.version_etag(current))
                return
            except WriteConflict:
                if attempt == self._MAX_DETECTED_ISSUE_WRITE_ATTEMPTS - 1:
                    raise

    _SERVER_ASSIGNED_META = ("versionId", "lastUpdated")  # a backend may set these on write; nothing else is ours to skip

    @classmethod
    def _verify_persisted(cls, resource_type: str, sent: dict, read_back) -> None:
        got = read_back(sent["id"])
        if got is None:
            raise FhirPersistVerificationFailed(f"{resource_type}/{sent['id']} was not found after writing it")
        # Compare EVERY key we sent, including meta -- meta.tag (the synthetic-data-origin tag) is
        # application-authored content and must round-trip unchanged. Only the specific server-assigned
        # sub-fields inside meta (versionId, lastUpdated) are stripped from the read-back before comparing;
        # nothing else is given a pass.
        got_meta = {k: v for k, v in (got.get("meta") or {}).items() if k not in cls._SERVER_ASSIGNED_META}
        got_comparable = {**got, "meta": got_meta} if "meta" in got else got
        mismatches = {k: (v, got_comparable.get(k)) for k, v in sent.items() if got_comparable.get(k) != v}
        if mismatches:
            raise FhirPersistVerificationFailed(
                f"{resource_type}/{sent['id']} content mismatch after read-back: {sorted(mismatches)}")

    def run(self, patient_id: str) -> Analysis:
        """Synchronous convenience: analyze, then a best-effort explain. The HTTP API does these as two requests."""
        analysis = self.analyze(patient_id)
        try:
            explanation = self.explain(patient_id, analysis.analysis_id)
        except Exception as exc:  # noqa: BLE001 - explanation must never break the deterministic result
            log.warning("AI explanation failed for %s; returning deterministic result only (%s)",
                        patient_id, redact(f"{type(exc).__name__}: {exc}"))
            return analysis
        return analysis.model_copy(update={"ai_explanation": explanation})

    def latest(self, patient_id: str) -> Analysis | None:
        stored = self._repo.get_latest_analysis(patient_id)
        return Analysis.model_validate(stored) if stored else None
