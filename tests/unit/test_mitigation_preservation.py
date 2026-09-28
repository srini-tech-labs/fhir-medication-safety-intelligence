"""Mitigation preservation: re-persisting a DetectedIssue must keep provider-owned `mitigation[]` (written by SMART
Medication Reconciliation Intelligence) while replacing producer-owned fields, using If-Match against the version
just read and re-reading/re-merging on a concurrent change. Exercises the real decorated HealthLake repository
(`_store_errors`) against the fake HealthLake, not only a mocked service-level exception.
"""
from __future__ import annotations

import copy
import json

import httpx
import pytest

from app.container import build_container
from app.repository.app_state import AppStateStore
from app.repository.base import StoreUnavailable, WriteConflict
from app.repository.healthlake import HealthLakeFHIRRepository
from app.repository.healthlake_client import HealthLakeClient
from app.repository.local import LocalFHIRRepository
from app.services import fhir_outputs
from app.services.explanation.mock import MockExplanationService
from app.terminology import Terminology
from tests.api.test_persist import client_for, read_issue
from tests.conftest import PACKAGE_DIR
from tests.support.fake_healthlake import APP_KEY, DATASTORE_ID, FakeHealthLake, credentials

ISSUE = "di-p001-dl001"  # P001 has exactly one finding (DL-001)
DISPOSITION = {"action": {"text": "Therapy appropriate: continue with monitoring (synthetic)"},
               "date": "2026-09-26T10:00:00Z", "author": {"reference": "Practitioner/practitioner-smri-demo-01"}}


@pytest.fixture()
def fake():
    return FakeHealthLake()


def repo_for(fake, tmp_path, handler=None) -> HealthLakeFHIRRepository:
    transport = httpx.MockTransport(handler or fake.handle)
    client = HealthLakeClient(DATASTORE_ID, credentials=credentials(APP_KEY), transport=transport, sleep=lambda s: None)
    return HealthLakeFHIRRepository(client, Terminology.load(PACKAGE_DIR), AppStateStore(tmp_path / "hl-output"),
                                    write_fhir_outputs=True)


def container_for(repo, settings):
    return build_container(settings, repository=repo, explainer=MockExplanationService())


def smri_appends_mitigation(fake, issue_id: str, entry: dict = DISPOSITION) -> dict:
    """Simulates SMRI appending a provider disposition (a new version, as its own PUT would create)."""
    current = copy.deepcopy(fake.current("DetectedIssue", issue_id))
    current["mitigation"] = current.get("mitigation", []) + [entry]
    return fake._commit(current)


def detected_issue_puts(fake) -> list[dict]:
    return [r for r in fake.requests if r["method"] == "PUT" and r["path"].startswith("DetectedIssue/")]


# ---- the decorated repository path ----------------------------------------------------------------------------------

def test_412_surfaces_as_writeconflict_through_the_store_errors_decorator(fake, tmp_path, settings):
    repo = repo_for(fake, tmp_path)
    c = container_for(repo, settings)
    c.analyses.persist_to_fhir("P001", c.analyses.analyze("P001").analysis_id)  # version 1
    stale = fhir_outputs.version_etag(fake.current("DetectedIssue", ISSUE))
    smri_appends_mitigation(fake, ISSUE)  # version 2

    with pytest.raises(WriteConflict) as exc:
        repo.save_detected_issue(fhir_outputs.build_detected_issue("Patient/patient-p001",
                                                                    c.analyses.latest("P001").findings[0], "t"),
                                 if_match=stale)
    assert type(exc.value) is WriteConflict  # not converted to a plain StoreUnavailable by _store_errors
    assert fake.current("DetectedIssue", ISSUE)["meta"]["versionId"] == "2"  # nothing written


def test_other_store_failures_are_still_converted_to_storeunavailable(fake, tmp_path):
    repo = repo_for(fake, tmp_path)
    fake.fail_next = [403]
    with pytest.raises(StoreUnavailable) as exc:
        repo.save_detected_issue({"resourceType": "DetectedIssue", "id": ISSUE, "status": "final",
                                  "patient": {"reference": "Patient/patient-p001"}})
    assert not isinstance(exc.value, WriteConflict)


def test_first_persist_is_an_unconditional_put_as_before(fake, tmp_path, settings):
    c = container_for(repo_for(fake, tmp_path), settings)
    c.analyses.persist_to_fhir("P001", c.analyses.analyze("P001").analysis_id)
    (put,) = detected_issue_puts(fake)
    assert "if-match" not in put["headers"]
    assert "mitigation" not in fake.current("DetectedIssue", ISSUE)


# ---- preservation -----------------------------------------------------------------------------------------------------

def test_repersist_keeps_provider_mitigation_and_refreshes_producer_fields(fake, tmp_path, settings):
    c = container_for(repo_for(fake, tmp_path), settings)
    aid = c.analyses.analyze("P001").analysis_id
    c.analyses.persist_to_fhir("P001", aid)
    before = smri_appends_mitigation(fake, ISSUE)  # version 2 with a disposition

    c.analyses.persist_to_fhir("P001", aid)
    after = fake.current("DetectedIssue", ISSUE)
    assert after["mitigation"] == [DISPOSITION]  # provider-owned: carried verbatim
    assert after["meta"]["versionId"] == "3"
    for field in ("status", "code", "severity", "patient", "implicated", "detail", "reference"):
        assert after[field] == before[field]  # producer-owned: rebuilt from the analysis (same deterministic content)
    assert after["identifiedDateTime"] >= before["identifiedDateTime"]
    assert after["meta"]["tag"] == [fhir_outputs.DATA_ORIGIN_TAG]
    assert detected_issue_puts(fake)[-1]["headers"]["if-match"] == 'W/"2"'  # conditional on the version read


def test_producer_fields_replace_stale_values_but_mitigation_survives(fake, tmp_path, settings):
    c = container_for(repo_for(fake, tmp_path), settings)
    aid = c.analyses.analyze("P001").analysis_id
    c.analyses.persist_to_fhir("P001", aid)
    tampered = copy.deepcopy(fake.current("DetectedIssue", ISSUE))
    tampered.update(severity="low", detail="stale text", mitigation=[DISPOSITION])
    fake._commit(tampered)

    c.analyses.persist_to_fhir("P001", aid)
    after = fake.current("DetectedIssue", ISSUE)
    assert after["severity"] == "high" and after["detail"].startswith("DL-001:")
    assert after["mitigation"] == [DISPOSITION]


# ---- concurrency ------------------------------------------------------------------------------------------------------

def test_concurrent_disposition_between_read_and_put_is_reread_and_merged(fake, tmp_path, settings):
    second = dict(DISPOSITION, action={"text": "Deferred to follow-up visit (synthetic)"})
    raced = []

    def racing(request):
        if request.method == "PUT" and "/DetectedIssue/" in request.url.path and fake.current("DetectedIssue", ISSUE) and not raced:
            raced.append(True)
            smri_appends_mitigation(fake, ISSUE, second)  # SMRI writes after our read, before our PUT
        return fake.handle(request)

    c = container_for(repo_for(fake, tmp_path, racing), settings)
    aid = c.analyses.analyze("P001").analysis_id
    c.analyses.persist_to_fhir("P001", aid)  # creates v1 (no race on create)
    smri_appends_mitigation(fake, ISSUE)  # v2: first disposition
    raced.clear()

    result = c.analyses.persist_to_fhir("P001", aid)
    after = fake.current("DetectedIssue", ISSUE)
    assert result.status == "PERSISTED"
    assert after["mitigation"] == [DISPOSITION, second]  # nothing lost to the race
    statuses = [r["headers"].get("if-match") for r in detected_issue_puts(fake)[-2:]]
    assert statuses == ['W/"2"', 'W/"3"']  # stale attempt (412), then re-read and retried on the new version


def test_persistent_conflict_gives_up_after_three_attempts_with_storeunavailable(fake, tmp_path, settings):
    def always_racing(request):
        if request.method == "PUT" and "/DetectedIssue/" in request.url.path and fake.current("DetectedIssue", ISSUE):
            smri_appends_mitigation(fake, ISSUE)
        return fake.handle(request)

    c = container_for(repo_for(fake, tmp_path, always_racing), settings)
    aid = c.analyses.analyze("P001").analysis_id
    c.analyses.persist_to_fhir("P001", aid)
    puts_before = len(detected_issue_puts(fake))
    with pytest.raises(StoreUnavailable) as exc:
        c.analyses.persist_to_fhir("P001", aid)
    assert isinstance(exc.value, WriteConflict)
    assert len(detected_issue_puts(fake)) - puts_before == 3
    assert all(m == DISPOSITION for m in fake.current("DetectedIssue", ISSUE)["mitigation"])  # only SMRI's writes landed


def test_mitigation_added_after_put_does_not_fail_read_back_verification(fake, tmp_path, settings):
    wrote = []

    def append_after_put(request):
        resp = fake.handle(request)
        if request.method == "PUT" and "/DetectedIssue/" in request.url.path:
            wrote.append(True)
        elif request.method == "GET" and "/DetectedIssue/" in request.url.path and wrote:
            wrote.clear()
            smri_appends_mitigation(fake, ISSUE)
            resp = fake.handle(request)  # read-back now sees the provider's newer version
        return resp

    c = container_for(repo_for(fake, tmp_path, append_after_put), settings)
    aid = c.analyses.analyze("P001").analysis_id
    assert c.analyses.persist_to_fhir("P001", aid).status == "PERSISTED"


def test_only_existing_app_role_actions_are_used(fake, tmp_path, settings):
    c = container_for(repo_for(fake, tmp_path), settings)
    aid = c.analyses.analyze("P001").analysis_id
    c.analyses.persist_to_fhir("P001", aid)
    smri_appends_mitigation(fake, ISSUE)
    c.analyses.persist_to_fhir("P001", aid)
    assert {r["action"] for r in fake.requests} <= {"ReadResource", "SearchWithGet", "UpdateResource"}


def test_api_maps_an_exhausted_conflict_to_the_existing_generic_503(settings):
    class AlwaysConflicting(LocalFHIRRepository):
        def save_detected_issue(self, issue, *, if_match=None):
            raise WriteConflict("DetectedIssue changed since it was read")

    client = client_for(settings, repository=AlwaysConflicting(settings.package_dir, settings.output_dir))
    aid = client.post("/v1/patients/P001/analyses").json()["analysisId"]
    r = client.post(f"/v1/patients/P001/analyses/{aid}/persist")
    assert r.status_code == 503 and "changed since" not in r.text


# ---- local (file) repository and pure helpers ------------------------------------------------------------------------

def test_local_repository_also_preserves_mitigation(settings):
    client = client_for(settings)
    aid = client.post("/v1/patients/P001/analyses").json()["analysisId"]
    client.post(f"/v1/patients/P001/analyses/{aid}/persist")
    path = settings.output_dir / "detected_issues" / f"{ISSUE}.json"
    doc = json.loads(path.read_text()) | {"mitigation": [DISPOSITION]}
    path.write_text(json.dumps(doc))
    assert client.post(f"/v1/patients/P001/analyses/{aid}/persist").status_code == 200
    assert read_issue(settings, ISSUE)["mitigation"] == [DISPOSITION]


def test_preserve_provider_fields():
    new = {"resourceType": "DetectedIssue", "id": "x", "severity": "high"}
    assert fhir_outputs.preserve_provider_fields(new, None) == new
    assert fhir_outputs.preserve_provider_fields(new, {"severity": "low", "mitigation": []}) == new
    merged = fhir_outputs.preserve_provider_fields(new, {"severity": "low", "detail": "old", "mitigation": [DISPOSITION]})
    assert merged == new | {"mitigation": [DISPOSITION]}
    assert fhir_outputs.PROVIDER_OWNED_FIELDS == ("mitigation",)


def test_version_etag():
    assert fhir_outputs.version_etag(None) is None
    assert fhir_outputs.version_etag({"meta": {}}) is None
    assert fhir_outputs.version_etag({"meta": {"versionId": "7"}}) == 'W/"7"'
