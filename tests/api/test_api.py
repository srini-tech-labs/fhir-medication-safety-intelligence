"""REST contract tests against the frozen mocks/schemas in the Phase 0 package."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from jsonschema import validate

from app.api.deps import get_container
from app.main import create_app
from tests.conftest import PACKAGE_DIR, load_json

MOCK = PACKAGE_DIR / "api" / "mock"
SCHEMAS = PACKAGE_DIR / "schemas"


@pytest.fixture()
def client(container):
    app = create_app()
    app.dependency_overrides[get_container] = lambda: container
    return TestClient(app)


def assert_subset(expected, actual, path="$"):
    """Every field in the frozen mock must be present and equal; extra (additive) fields are allowed."""
    if isinstance(expected, dict):
        assert isinstance(actual, dict), path
        for k, v in expected.items():
            assert k in actual, f"{path}.{k} missing"
            assert_subset(v, actual[k], f"{path}.{k}")
    elif isinstance(expected, list):
        assert isinstance(actual, list) and len(actual) == len(expected), path
        for i, (e, a) in enumerate(zip(expected, actual)):
            assert_subset(e, a, f"{path}[{i}]")
    else:
        assert expected == actual, f"{path}: {expected!r} != {actual!r}"


def test_health(client):
    assert client.get("/v1/health").json() == {"status": "ok"}


def test_patient_list_matches_frozen_mock(client):
    r = client.get("/v1/patients")
    assert r.status_code == 200
    assert r.json() == load_json(MOCK / "GET_v1_patients.json")


@pytest.mark.parametrize("pid", ["P001", "P008"])
def test_snapshot_matches_frozen_mock_and_schema(client, pid):
    body = client.get(f"/v1/patients/{pid}/snapshot").json()
    assert_subset(load_json(MOCK / f"GET_v1_patients_{pid}_snapshot.json"), body)
    validate(body, load_json(SCHEMAS / "patient-snapshot.schema.json"))


def test_snapshot_never_exposes_raw_fhir(client):
    text = json.dumps([client.get(f"/v1/patients/P00{n}/snapshot").json() for n in range(1, 10)])
    assert "resourceType" not in text and "MedicationRequest" not in text


def test_analysis_p001_matches_frozen_mock_and_schema(client):
    body = client.post("/v1/patients/P001/analyses").json()
    expected = load_json(MOCK / "POST_v1_patients_P001_analyses.json")
    expected.pop("aiExplanation")  # wording is not golden-tested
    assert_subset(expected, body)
    validate(body, load_json(SCHEMAS / "analysis-response.schema.json"))
    assert body["aiExplanation"] is None  # deterministic result only; the explanation is requested separately
    explanation = client.post(f"/v1/patients/P001/analyses/{body['analysisId']}/explanation").json()
    assert explanation["groundedInFindingsOnly"] is True


@pytest.mark.parametrize("pid", [f"P{n:03d}" for n in range(1, 11)])
def test_every_analysis_validates_against_schema(client, pid):
    body = client.post(f"/v1/patients/{pid}/analyses").json()
    validate(body, load_json(SCHEMAS / "analysis-response.schema.json"))
    assert body["aiExplanation"] is None
    explanation = client.post(f"/v1/patients/{pid}/analyses/{body['analysisId']}/explanation").json()
    assert explanation["mode"] == "mock" and explanation["fallbackCode"] is None
    latest = client.get(f"/v1/patients/{pid}/analyses/latest").json()  # the saved record now carries it
    validate(latest, load_json(SCHEMAS / "analysis-response.schema.json"))
    assert latest["aiExplanation"] == explanation
    assert {k: v for k, v in latest.items() if k != "aiExplanation"} == {k: v for k, v in body.items() if k != "aiExplanation"}


def test_latest_analysis_404_then_200(client):
    assert client.get("/v1/patients/P002/analyses/latest").status_code == 404
    posted = client.post("/v1/patients/P002/analyses").json()
    latest = client.get("/v1/patients/P002/analyses/latest")
    assert latest.status_code == 200 and latest.json() == posted
    second = client.post("/v1/patients/P002/analyses").json()
    assert (posted["analysisId"], second["analysisId"]) == ("AN-P002-001", "AN-P002-002")
    assert client.get("/v1/patients/P002/analyses/latest").json()["analysisId"] == "AN-P002-002"


def test_document_endpoint(client):
    doc = client.get("/v1/patients/P008/documents/DOC-P008-01").json()
    assert doc["type"] == "Discharge Summary" and "Potassium was elevated" in doc["text"]
    listed = client.get("/v1/patients/P008/snapshot").json()["documents"]
    assert [d["id"] for d in listed] == ["DOC-P008-01"]


def test_note_patients_are_exactly_p002_p006_p008_p010(client):
    with_notes = [f"P{n:03d}" for n in range(1, 11)
                  if client.get(f"/v1/patients/P{n:03d}/snapshot").json()["documents"]]
    assert with_notes == ["P002", "P006", "P008", "P010"]


@pytest.mark.parametrize("method,url", [
    ("get", "/v1/patients/P999/snapshot"),
    ("post", "/v1/patients/P999/analyses"),
    ("get", "/v1/patients/P999/analyses/latest"),
    ("get", "/v1/patients/P999/documents/DOC-P999-01"),
    ("get", "/v1/patients/P001/documents/DOC-P001-01"),
    ("get", "/v1/patients/..%2f..%2fetc/snapshot"),
])
def test_unknown_resources_404(client, method, url):
    assert getattr(client, method)(url).status_code == 404


def test_derived_fhir_resources_match_frozen_examples(client, container, settings):
    analysis_id = client.post("/v1/patients/P008/analyses").json()["analysisId"]
    persisted = client.post(f"/v1/patients/P008/analyses/{analysis_id}/persist")
    assert persisted.status_code == 200, persisted.text
    body = persisted.json()
    assert body["status"] == "PERSISTED" and body["riskAssessmentId"] == "ra-p008-001"
    assert set(body["detectedIssueIds"]) == {"di-p008-dl001", "di-p008-ddi003"}

    out = settings.output_dir
    di = json.loads((out / "detected_issues" / "di-p008-dl001.json").read_text())
    ra = json.loads((out / "risk_assessments" / "ra-p008-001.json").read_text())
    ex_di = load_json(PACKAGE_DIR / "fhir" / "examples" / "DetectedIssue-P008-example.json")
    ex_ra = load_json(PACKAGE_DIR / "fhir" / "examples" / "RiskAssessment-P008-example.json")
    di.pop("identifiedDateTime"), ex_di.pop("identifiedDateTime")
    ra.pop("occurrenceDateTime"), ex_ra.pop("occurrenceDateTime")
    assert di == ex_di
    assert ra == ex_ra
    assert (out / "detected_issues" / "di-p008-ddi003.json").exists()  # the MODERATE finding too


def test_normal_analyze_never_writes_derived_fhir_resources(client, settings):
    """analyze() is read-only w.r.t. the clinical store now -- only the explicit /persist route writes.
    Regression: this exact write used to happen unconditionally inside analyze()."""
    client.post("/v1/patients/P008/analyses")
    out = settings.output_dir
    assert not (out / "detected_issues").exists()
    assert not (out / "risk_assessments").exists()


def test_frozen_package_is_not_written_to(client):
    before = sorted(p.name for p in PACKAGE_DIR.rglob("*"))
    client.post("/v1/patients/P001/analyses")
    assert sorted(p.name for p in PACKAGE_DIR.rglob("*")) == before
