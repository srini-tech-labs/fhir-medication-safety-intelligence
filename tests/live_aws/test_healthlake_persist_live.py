"""LIVE write-back to a real AWS HealthLake datastore. Opt-in, gated SEPARATELY from the read-only live suite
(tests/live_aws/test_healthlake_live.py) because this one actually writes -- a normal read-only live run must
never accidentally trigger it.

    RUN_HEALTHLAKE_LIVE=1 RUN_HEALTHLAKE_WRITE_LIVE=1 HEALTHLAKE_DATASTORE_ID=<id> \
        HEALTHLAKE_ROLE_ARN=<App role ARN> AWS_PROFILE=<profile> \
        backend/.venv/bin/python -m pytest -m live_aws tests/live_aws/test_healthlake_persist_live.py -o addopts="-q"

Requires the least-privilege healthlake:UpdateResource-only IAM grant proposed in the persist-feature deployment
plan to already be applied -- until then this fails with AccessDenied, which is the expected, correct behavior
of the safety architecture (report the IAM change, wait for approval, only then can this pass).

test_persist_writes_and_verifies_a_real_detected_issue_and_risk_assessment is deliberately the FIRST persist
ever run for its DetectedIssue id against a fresh datastore -- i.e. the PUT is create-shaped (the id does not
yet exist). AWS HealthLake documents PUT as the FHIR "update" interaction, which creates the initial version
itself when the id is new, so UpdateResource alone is expected to be sufficient. If this test instead fails
with AccessDenied naming CreateResource, THAT is the live demonstration that CreateResource is actually
required -- add it to lambda-access.json.tpl only then, not preemptively.
"""
from __future__ import annotations

import os

import pytest

from app.config import Settings
from app.container import build_container
from app.repository.healthlake import HealthLakeFHIRRepository
from app.services.explanation.mock import MockExplanationService
from tests.conftest import PACKAGE_DIR

pytestmark = [
    pytest.mark.live_aws,
    pytest.mark.skipif(
        os.getenv("RUN_HEALTHLAKE_LIVE") != "1" or os.getenv("RUN_HEALTHLAKE_WRITE_LIVE") != "1"
        or not os.getenv("HEALTHLAKE_DATASTORE_ID"),
        reason="set RUN_HEALTHLAKE_LIVE=1, RUN_HEALTHLAKE_WRITE_LIVE=1 and HEALTHLAKE_DATASTORE_ID to write to real AWS"),
]


@pytest.fixture(scope="module")
def write_container(tmp_path_factory):
    base = Settings.from_env()
    cfg = Settings(**{**base.__dict__, "data_backend": "healthlake", "package_dir": PACKAGE_DIR,
                      "output_dir": tmp_path_factory.mktemp("hl-write-live-output"), "explanation_mode": "mock",
                      "healthlake_write_outputs": True})  # the ONLY test file in this repo that sets this True
    repo = HealthLakeFHIRRepository.from_settings(cfg)
    return build_container(cfg, repository=repo, explainer=MockExplanationService())


def test_persist_writes_and_verifies_a_real_detected_issue_and_risk_assessment(write_container):
    analysis = write_container.analyses.analyze("P001")
    result = write_container.analyses.persist_to_fhir("P001", analysis.analysis_id)
    assert result.status == "PERSISTED"
    assert result.detected_issue_ids == [analysis.findings[0].fhir.detected_issue_id]
    assert result.risk_assessment_id == analysis.risk_assessment.id

    client = write_container.repository._client
    issue = client.read("DetectedIssue", result.detected_issue_ids[0])
    assert issue["patient"]["reference"] == "Patient/patient-p001"
    assert issue["meta"]["tag"][0]["code"] == "synthetic"  # never mistakable for a real clinical write


def test_repeating_persist_against_real_healthlake_is_idempotent(write_container):
    analysis = write_container.analyses.analyze("P001")
    first = write_container.analyses.persist_to_fhir("P001", analysis.analysis_id)
    second = write_container.analyses.persist_to_fhir("P001", analysis.analysis_id)
    assert first.detected_issue_ids == second.detected_issue_ids
    assert first.risk_assessment_id == second.risk_assessment_id
    client = write_container.repository._client
    versioned = client.read("DetectedIssue", first.detected_issue_ids[0])
    assert int(versioned["meta"]["versionId"]) >= 2  # the second PUT created a new version, same logical resource


def test_negative_case_writes_no_detected_issue_for_p009(write_container):
    analysis = write_container.analyses.analyze("P009")
    result = write_container.analyses.persist_to_fhir("P009", analysis.analysis_id)
    assert result.detected_issue_ids == []
    client = write_container.repository._client
    ra = client.read("RiskAssessment", result.risk_assessment_id)
    assert ra["prediction"][0]["qualitativeRisk"]["coding"][0]["code"] == "none"
