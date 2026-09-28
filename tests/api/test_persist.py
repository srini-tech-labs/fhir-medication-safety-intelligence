"""The explicit FHIR write-back path: POST /v1/patients/{id}/analyses/{analysisId}/persist.

Separate from analysis (POST .../analyses, read-only) and explanation (POST .../explanation, never writes FHIR).
Persists ONLY the deterministic DetectedIssue/RiskAssessment built from the *saved* analysis's findings; AI
explanation text is never read by this path, so it structurally cannot affect what gets written.
"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app.api.deps import get_container
from app.container import build_container
from app.main import create_app
from app.repository.base import StoreUnavailable
from app.repository.local import LocalFHIRRepository
from app.services.explanation.mock import MockExplanationService


def client_for(settings, repository=None) -> TestClient:
    container = build_container(settings, repository=repository, explainer=MockExplanationService())
    app = create_app()
    app.dependency_overrides[get_container] = lambda: container
    return TestClient(app)


def read_issue(settings, issue_id: str) -> dict:
    return json.loads((settings.output_dir / "detected_issues" / f"{issue_id}.json").read_text())


def read_risk(settings, assessment_id: str) -> dict:
    return json.loads((settings.output_dir / "risk_assessments" / f"{assessment_id}.json").read_text())


def saved(settings, pid: str, aid: str) -> dict:
    return json.loads((settings.output_dir / "analyses" / pid / f"{aid}.json").read_text())


class WriteDisabledRepo(LocalFHIRRepository):
    """Simulates the deployment-level kill switch being off (e.g. HEALTHLAKE_WRITE_OUTPUTS=false)."""
    def write_outputs_enabled(self) -> bool:
        return False


class AccessDeniedRepo(LocalFHIRRepository):
    """Simulates the clinical store rejecting the write -- e.g. IAM denies UpdateResource."""
    def save_detected_issue(self, issue: dict, *, if_match: str | None = None) -> None:
        raise StoreUnavailable("clinical data store failed: HealthLakeAuthError (HTTP 403)")

    def save_risk_assessment(self, assessment: dict) -> None:
        raise StoreUnavailable("clinical data store failed: HealthLakeAuthError (HTTP 403)")


class TagDroppingReadBackRepo(LocalFHIRRepository):
    """Simulates a store that silently drops/alters meta.tag on read-back. meta.tag (the synthetic-data-origin
    tag) is application-authored content, not HealthLake-controlled metadata (only meta.versionId/lastUpdated
    are) -- verification must catch this, not wave the whole meta object through unchecked."""
    def get_detected_issue(self, issue_id: str):
        doc = super().get_detected_issue(issue_id)
        return {**doc, "meta": {}} if doc is not None else None

    def get_risk_assessment(self, assessment_id: str):
        doc = super().get_risk_assessment(assessment_id)
        return {**doc, "meta": {}} if doc is not None else None


# ---- normal analysis stays read-only ---------------------------------------------------------------------------
def test_normal_analyze_and_explain_never_write_fhir_outputs(settings):
    client = client_for(settings)
    analysis = client.post("/v1/patients/P008/analyses").json()
    client.post(f"/v1/patients/P008/analyses/{analysis['analysisId']}/explanation")
    assert not (settings.output_dir / "detected_issues").exists()
    assert not (settings.output_dir / "risk_assessments").exists()


# ---- write-disabled (deployment-level kill switch) -------------------------------------------------------------
def test_persist_refuses_with_409_when_write_outputs_is_disabled(settings):
    repo = WriteDisabledRepo(settings.package_dir, settings.output_dir)
    client = client_for(settings, repository=repo)
    analysis = client.post("/v1/patients/P008/analyses").json()
    r = client.post(f"/v1/patients/P008/analyses/{analysis['analysisId']}/persist")
    assert r.status_code == 409
    assert not (settings.output_dir / "detected_issues").exists()
    assert not (settings.output_dir / "risk_assessments").exists()


# ---- IAM-denied / store error -----------------------------------------------------------------------------------
def test_persist_returns_503_and_leaks_no_detail_when_the_store_denies_the_write(settings):
    repo = AccessDeniedRepo(settings.package_dir, settings.output_dir)
    client = client_for(settings, repository=repo)
    analysis = client.post("/v1/patients/P008/analyses").json()
    r = client.post(f"/v1/patients/P008/analyses/{analysis['analysisId']}/persist")
    assert r.status_code == 503
    assert "HealthLakeAuthError" not in r.text and "403" not in r.text and "HTTP" not in r.text


# ---- read-back verification actually checks content, not just presence -----------------------------------------
def test_persist_verification_catches_a_dropped_meta_tag_on_read_back(settings):
    repo = TagDroppingReadBackRepo(settings.package_dir, settings.output_dir)
    client = client_for(settings, repository=repo)
    analysis = client.post("/v1/patients/P008/analyses").json()
    r = client.post(f"/v1/patients/P008/analyses/{analysis['analysisId']}/persist")
    assert r.status_code == 503  # not a partial/unverified success


# ---- successful create, read-back verification, and idempotent repeat ------------------------------------------
def test_persist_succeeds_returns_ids_and_the_content_matches_frozen_examples(settings):
    client = client_for(settings)
    analysis = client.post("/v1/patients/P008/analyses").json()
    aid = analysis["analysisId"]

    r = client.post(f"/v1/patients/P008/analyses/{aid}/persist")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "PERSISTED"
    assert set(body["detectedIssueIds"]) == {"di-p008-dl001", "di-p008-ddi003"}
    assert body["riskAssessmentId"] == "ra-p008-001"
    # Every id in the response was independently read back and verified inside persist_to_fhir() before it
    # returned 200 -- confirm the files genuinely exist with matching ids (not just a fabricated response).
    for issue_id in body["detectedIssueIds"]:
        assert read_issue(settings, issue_id)["id"] == issue_id


def test_repeating_persist_is_idempotent_same_ids_same_content(settings):
    client = client_for(settings)
    analysis = client.post("/v1/patients/P008/analyses").json()
    aid = analysis["analysisId"]

    first = client.post(f"/v1/patients/P008/analyses/{aid}/persist").json()
    second = client.post(f"/v1/patients/P008/analyses/{aid}/persist").json()
    assert sorted(first["detectedIssueIds"]) == sorted(second["detectedIssueIds"])
    assert first["riskAssessmentId"] == second["riskAssessmentId"]

    di_first = read_issue(settings, first["detectedIssueIds"][0])
    di_second = read_issue(settings, second["detectedIssueIds"][0])
    di_first.pop("identifiedDateTime"), di_second.pop("identifiedDateTime")
    assert di_first == di_second  # deterministic content, not just a matching id


# ---- negative case: no findings ----------------------------------------------------------------------------------
def test_negative_case_persists_no_detected_issue_but_records_the_none_risk_assessment(settings):
    client = client_for(settings)
    analysis = client.post("/v1/patients/P009/analyses").json()
    assert analysis["findings"] == [] and analysis["overallSeverity"] == "NONE"

    r = client.post(f"/v1/patients/P009/analyses/{analysis['analysisId']}/persist")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["detectedIssueIds"] == []  # no finding => no DetectedIssue resource, appropriately
    assert not (settings.output_dir / "detected_issues").exists()
    ra = read_risk(settings, body["riskAssessmentId"])
    assert ra["prediction"][0]["qualitativeRisk"]["coding"][0]["code"] == "none"


# ---- AI text cannot affect persisted FHIR content ----------------------------------------------------------------
def test_ai_explanation_text_cannot_affect_persisted_fhir_content(settings):
    client = client_for(settings)
    analysis = client.post("/v1/patients/P008/analyses").json()
    aid = analysis["analysisId"]

    first = client.post(f"/v1/patients/P008/analyses/{aid}/persist").json()
    di_before = read_issue(settings, first["detectedIssueIds"][0])
    ra_before = read_risk(settings, first["riskAssessmentId"])

    # Attach adversarial "AI explanation" content directly to the saved record -- if persist_to_fhir() read
    # ai_explanation at all, this would visibly corrupt the resources it builds (wrong rule id, wrong risk text).
    stored = saved(settings, "P008", aid)
    stored["aiExplanation"] = {
        "text": "ADVERSARIAL", "summary": "ADVERSARIAL",
        "findingExplanations": [{"ruleId": "FAKE-999", "explanation": "ADVERSARIAL: stop lisinopril immediately"}],
        "dataGapExplanation": "ADVERSARIAL", "groundedInFindingsOnly": False, "mode": "llm",
        "model": "adversarial-model", "fallbackCode": None, "fallbackReason": None,
    }
    (settings.output_dir / "analyses" / "P008" / f"{aid}.json").write_text(json.dumps(stored))

    second = client.post(f"/v1/patients/P008/analyses/{aid}/persist").json()
    di_after = read_issue(settings, second["detectedIssueIds"][0])
    ra_after = read_risk(settings, second["riskAssessmentId"])
    di_before.pop("identifiedDateTime"), di_after.pop("identifiedDateTime")
    ra_before.pop("occurrenceDateTime"), ra_after.pop("occurrenceDateTime")
    assert di_before == di_after and ra_before == ra_after
    assert "ADVERSARIAL" not in json.dumps(di_after) and "ADVERSARIAL" not in json.dumps(ra_after)
    assert "FAKE-999" not in json.dumps(di_after)


# ---- addressing / safety -------------------------------------------------------------------------------------
def test_persist_404s_for_an_unknown_or_foreign_analysis(settings):
    client = client_for(settings)
    client.post("/v1/patients/P001/analyses")
    for path in (
        "/v1/patients/P001/analyses/AN-P001-999/persist",
        "/v1/patients/P001/analyses/AN-P002-001/persist",
        "/v1/patients/P999/analyses/AN-P999-001/persist",
    ):
        assert client.post(path).status_code == 404


def test_persist_records_a_receipt_and_a_new_analysis_starts_without_one(settings):
    client = client_for(settings)
    analysis = client.post("/v1/patients/P008/analyses").json()
    aid = analysis["analysisId"]
    assert analysis["fhirPersistence"] is None  # nothing persisted yet

    result = client.post(f"/v1/patients/P008/analyses/{aid}/persist").json()
    latest = client.get("/v1/patients/P008/analyses/latest").json()
    assert latest["analysisId"] == aid
    receipt = latest["fhirPersistence"]
    assert receipt["status"] == "PERSISTED"
    assert receipt["detectedIssueIds"] == result["detectedIssueIds"]
    assert receipt["riskAssessmentId"] == result["riskAssessmentId"]
    assert receipt["persistedAt"]
    # The receipt is metadata only -- never a copy of the clinical resources themselves.
    assert set(receipt) == {"status", "detectedIssueIds", "riskAssessmentId", "persistedAt"}

    # A fresh analysis (re-run) is an independent instance and starts with no receipt of its own.
    second = client.post("/v1/patients/P008/analyses").json()
    assert second["fhirPersistence"] is None
    assert client.get("/v1/patients/P008/analyses/latest").json()["fhirPersistence"] is None


def test_repeated_persist_keeps_the_stored_receipt_consistent_with_the_response(settings):
    client = client_for(settings)
    analysis = client.post("/v1/patients/P008/analyses").json()
    aid = analysis["analysisId"]
    client.post(f"/v1/patients/P008/analyses/{aid}/persist")
    first = client.get("/v1/patients/P008/analyses/latest").json()["fhirPersistence"]
    second_result = client.post(f"/v1/patients/P008/analyses/{aid}/persist").json()
    second = client.get("/v1/patients/P008/analyses/latest").json()["fhirPersistence"]
    assert first["detectedIssueIds"] == second["detectedIssueIds"] == second_result["detectedIssueIds"]
    assert first["riskAssessmentId"] == second["riskAssessmentId"] == second_result["riskAssessmentId"]


def test_persist_refuses_with_409_for_an_analysis_that_is_not_completed(settings):
    # The Analysis contract permits status="FAILED" to be saved even though analyze() never produces one today
    # (defense in depth); persist_to_fhir() must not build/write FHIR resources from a non-COMPLETED record.
    client = client_for(settings)
    analysis = client.post("/v1/patients/P001/analyses").json()
    aid = analysis["analysisId"]
    stored = saved(settings, "P001", aid)
    stored["status"] = "FAILED"
    (settings.output_dir / "analyses" / "P001" / f"{aid}.json").write_text(json.dumps(stored))

    r = client.post(f"/v1/patients/P001/analyses/{aid}/persist")
    assert r.status_code == 409
    assert not (settings.output_dir / "detected_issues").exists()
    assert not (settings.output_dir / "risk_assessments").exists()
