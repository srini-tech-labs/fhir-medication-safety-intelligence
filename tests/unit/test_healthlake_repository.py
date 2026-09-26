"""HealthLakeFHIRRepository against the FakeHealthLake: must be indistinguishable from LocalFHIRRepository for all ten patients."""
from __future__ import annotations

import pytest
from botocore.credentials import Credentials

from app.config import Settings
from app.container import build_container
from app.repository.base import PatientNotFound, StoreUnavailable
from app.repository.factory import create_repository
from app.repository.healthlake import HealthLakeFHIRRepository
from app.repository.healthlake_client import HealthLakeAuthError, HealthLakeClient, HealthLakeError
from app.repository.app_state import AppStateStore
from app.services.explanation.mock import MockExplanationService
from app.terminology import Terminology
from tests.conftest import PACKAGE_DIR
from tests.support.fake_healthlake import APP_KEY, DATASTORE_ID, FakeHealthLake, credentials

PATIENTS = [f"P{n:03d}" for n in range(1, 11)]
FORBIDDEN_ACTIONS = {"GetHistoryByResourceId", "VersionReadResource", "ProcessBundle", "DeleteResource"}


@pytest.fixture()
def fake():
    return FakeHealthLake(page_size=5)


def make_repo(fake, tmp_path, **kw) -> HealthLakeFHIRRepository:
    client = HealthLakeClient(DATASTORE_ID, credentials=credentials(APP_KEY), transport=fake.transport(), sleep=lambda s: None)
    return HealthLakeFHIRRepository(client, Terminology.load(PACKAGE_DIR), AppStateStore(tmp_path / "hl-output"), **kw)


@pytest.fixture()
def repo(fake, tmp_path):
    return make_repo(fake, tmp_path)


@pytest.fixture()
def hl_container(repo, settings):
    return build_container(settings, repository=repo, explainer=MockExplanationService())


def dump(model):
    return model.model_dump(mode="json", by_alias=True)


# ---- parity with the local repository ----------------------------------------------------------------------
def test_patient_list_matches_local(repo, container):
    assert repo.get_patients() == container.repository.get_patients()
    assert [p.id for p in repo.get_patients()] == PATIENTS


@pytest.mark.parametrize("pid", PATIENTS)
def test_domain_objects_match_local(repo, container, pid):
    local = container.repository
    assert repo.get_patient(pid) == local.get_patient(pid)
    assert repo.get_medications(pid) == local.get_medications(pid)
    assert repo.get_observations(pid) == local.get_observations(pid)
    assert repo.get_documents(pid) == local.get_documents(pid)


@pytest.mark.parametrize("pid", PATIENTS)
def test_snapshot_and_analysis_match_local_exactly(container, hl_container, pid):
    assert dump(hl_container.snapshots.snapshot(pid)) == dump(container.snapshots.snapshot(pid))
    volatile = {"generatedAt"}
    a_local, a_hl = dump(container.analyses.analyze(pid)), dump(hl_container.analyses.analyze(pid))
    assert {k: v for k, v in a_hl.items() if k not in volatile} == {k: v for k, v in a_local.items() if k not in volatile}


def test_golden_facts_through_healthlake(hl_container):
    run = hl_container.analyses.analyze
    assert [f.rule_id for f in run("P002").findings] and run("P001").overall_severity == "HIGH"
    p6 = run("P006")
    assert [f.rule_id for f in p6.findings] == ["DDI-003"] and [g.rule_id for g in p6.data_gaps] == ["DG-001"]
    p8 = run("P008")
    assert [(f.rule_id, f.severity) for f in p8.findings] == [("DL-001", "HIGH"), ("DDI-003", "MODERATE")]
    p9, p10 = run("P009"), run("P010")
    assert (p9.overall_severity, p9.findings, p9.data_gaps) == ("NONE", [], [])
    assert p10.overall_severity == "NEEDS_DATA" and hl_container.snapshots.snapshot("P010").labs == []


def test_note_text_comes_from_binary_and_equals_local(repo, container):
    (doc,) = repo.get_documents("P002")
    assert doc.text == container.repository.get_documents("P002")[0].text and doc.text.strip()


# ---- what the repository is allowed to do at runtime ---------------------------------------------------------
def test_runtime_needs_only_the_app_role_actions_and_never_writes_derived_resources_by_default(fake, hl_container):
    for pid in PATIENTS:
        hl_container.analyses.analyze(pid)
        hl_container.snapshots.snapshot(pid)
    actions = {r["action"] for r in fake.requests}
    assert actions <= {"ReadResource", "SearchWithGet"}  # no create/update, and none of the Verifier-only actions
    assert not actions & FORBIDDEN_ACTIONS
    assert all(r["key"] == APP_KEY for r in fake.requests)
    assert fake.all_current("DetectedIssue") == [] and fake.all_current("RiskAssessment") == []
    assert sum(len(v) for t in fake.store.values() for v in t.values()) == 53  # nothing written, every resource still v1


def test_all_frozen_resources_stay_at_version_1_after_analysis(fake, hl_container):
    for pid in PATIENTS:
        hl_container.analyses.analyze(pid)
    assert {v[-1]["resource"]["meta"]["versionId"] for t in fake.store.values() for v in t.values()} == {"1"}


def test_fhir_outputs_are_written_only_when_explicitly_enabled(fake, tmp_path, settings):
    """analyze() itself never writes FHIR outputs (that's the separate, explicit persist_to_fhir() path) --
    even with write_fhir_outputs=True and a real DetectedIssue id already computed on the finding."""
    repo = make_repo(fake, tmp_path, write_fhir_outputs=True)
    c = build_container(settings, repository=repo, explainer=MockExplanationService())
    analysis = c.analyses.analyze("P001")
    assert fake.all_current("DetectedIssue") == [] and fake.all_current("RiskAssessment") == []

    result = c.analyses.persist_to_fhir("P001", analysis.analysis_id)
    (issue,) = fake.all_current("DetectedIssue")
    assert issue["id"] == analysis.findings[0].fhir.detected_issue_id and issue["patient"]["reference"] == "Patient/patient-p001"
    assert result.detected_issue_ids == [issue["id"]] and result.status == "PERSISTED"
    assert len(fake.all_current("RiskAssessment")) == 1 and result.risk_assessment_id == analysis.risk_assessment.id

    c.analyses.persist_to_fhir("P001", analysis.analysis_id)  # deterministic id => idempotent upsert, history preserved
    assert len(fake.all_current("DetectedIssue")) == 1
    assert fake.store["DetectedIssue"][issue["id"]][-1]["resource"]["meta"]["versionId"] == "2"
    assert {r["action"] for r in fake.requests} <= {"ReadResource", "SearchWithGet", "UpdateResource"}


def test_write_disabled_repo_still_supports_app_state(hl_container, repo):
    a = hl_container.analyses.analyze("P001")
    assert repo.get_analysis("P001", a.analysis_id)["analysisId"] == a.analysis_id
    assert repo.get_latest_analysis("P001")["analysisId"] == a.analysis_id
    assert repo.next_analysis_number("P001") == 2
    assert repo.get_analysis("P001", "AN-P001-999") is None


# ---- failure behaviour ----------------------------------------------------------------------------------------
def test_unknown_patient_raises_patient_not_found(repo):
    for method in (repo.get_patient, repo.get_medications, repo.get_observations, repo.get_documents,
                   repo.get_latest_analysis):
        with pytest.raises(PatientNotFound):
            method("P999")
    with pytest.raises(PatientNotFound):
        repo.get_analysis("P999", "AN-P999-001")


def test_duplicate_identifier_is_an_error_not_a_silent_pick(fake, repo):
    dup = dict(fake.current("Patient", "patient-p001"), id="patient-p001-dup")
    fake._commit(dup)
    with pytest.raises(StoreUnavailable, match="clinical data store failed") as exc:
        repo.get_patient("P001")
    assert isinstance(exc.value.__cause__, HealthLakeError) and "matched 2 Patient" in str(exc.value.__cause__)


def test_patients_with_a_foreign_identifier_system_are_not_listed(fake, repo):
    stranger = dict(fake.current("Patient", "patient-p001"), id="stranger")
    stranger["identifier"] = [{"system": "https://other.example/mrn", "value": "P001"}]
    fake._commit(stranger)
    assert [p.id for p in repo.get_patients()] == PATIENTS


def test_access_denied_surfaces_as_auth_error_not_empty_data(fake, tmp_path):
    client = HealthLakeClient(DATASTORE_ID, credentials=lambda: Credentials(APP_KEY, "bad"), transport=fake.transport(),
                              sleep=lambda s: None)
    repo = HealthLakeFHIRRepository(client, Terminology.load(PACKAGE_DIR), AppStateStore(tmp_path))
    with pytest.raises(StoreUnavailable, match="HealthLakeAuthError") as exc:
        repo.get_patient("P001")
    assert isinstance(exc.value.__cause__, HealthLakeAuthError)


def test_missing_binary_is_skipped_like_local(fake, repo):
    del fake.store["Binary"]["bin-p002-note"]
    assert repo.get_documents("P002") == []


# ---- query strategy --------------------------------------------------------------------------------------------
def test_one_search_plus_binary_reads_per_patient_and_a_short_cache(fake, repo):
    repo.get_patient("P002"); repo.get_medications("P002"); repo.get_documents("P002")
    paths = [(r["method"], r["path"]) for r in fake.requests]
    assert paths.count(("GET", "Patient")) == 1 and ("GET", "Binary/bin-p002-note") in paths and len(paths) == 2


def test_cache_expires(fake, tmp_path):
    now = [0.0]
    repo = make_repo(fake, tmp_path, cache_ttl=10.0, clock=lambda: now[0])
    repo.get_patient("P001"); repo.get_patient("P001")
    assert len(fake.requests) == 1
    now[0] = 11.0
    repo.get_patient("P001")
    assert len(fake.requests) == 2


def test_revinclude_spelling_falls_back_to_subject_then_to_per_type_searches(fake, repo, container):
    fake.unsupported_revinclude = {"patient"}
    assert repo.get_medications("P008") == container.repository.get_medications("P008")
    assert any("_revinclude=MedicationRequest%3Asubject" in r["query"] for r in fake.requests)
    fake.requests.clear()
    repo._cache.clear()
    fake.unsupported_revinclude = {"patient", "subject"}
    assert repo.get_observations("P008") == container.repository.get_observations("P008")
    assert {r["path"] for r in fake.requests} >= {"MedicationRequest", "Observation", "Encounter", "DocumentReference"}


# ---- wiring --------------------------------------------------------------------------------------------------------
def test_factory_builds_the_healthlake_repository_from_settings(tmp_path):
    s = Settings.from_env()
    cfg = Settings(**{**s.__dict__, "data_backend": "healthlake", "healthlake_datastore_id": DATASTORE_ID,
                      "output_dir": tmp_path, "package_dir": PACKAGE_DIR})
    assert isinstance(create_repository(cfg), HealthLakeFHIRRepository)


def test_factory_requires_a_datastore_id(settings):
    cfg = Settings(**{**settings.__dict__, "data_backend": "healthlake", "healthlake_datastore_id": None})
    with pytest.raises(ValueError, match="HEALTHLAKE_DATASTORE_ID"):
        create_repository(cfg)


def test_outputs_flag_is_off_by_default(monkeypatch):
    monkeypatch.delenv("HEALTHLAKE_WRITE_OUTPUTS", raising=False)
    assert Settings.from_env().healthlake_write_outputs is False
    monkeypatch.setenv("HEALTHLAKE_WRITE_OUTPUTS", "true")
    assert Settings.from_env().healthlake_write_outputs is True
