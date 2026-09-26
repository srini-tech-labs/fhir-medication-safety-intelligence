"""The Lambda entry point (API Gateway REST proxy events -> the same FastAPI app), on the in-memory HealthLake and DynamoDB fakes."""
from __future__ import annotations

import json
import logging

import pytest

from app.api.deps import get_container
from app.container import build_container
from app.lambda_handler import handler
from app.main import app
from app.repository.app_state_dynamodb import DynamoAppStateStore
from app.repository.healthlake import HealthLakeFHIRRepository
from app.repository.healthlake_client import HealthLakeClient
from app.services.explanation.mock import MockExplanationService
from app.terminology import Terminology
from tests.conftest import PACKAGE_DIR
from tests.support.fake_dynamodb import TABLE, FakeDynamoDB
from tests.support.fake_healthlake import APP_KEY, DATASTORE_ID, FakeHealthLake, credentials

PATIENTS = [f"P{n:03d}" for n in range(1, 11)]
ORIGIN = "http://localhost:5173"


def event(method: str, path: str, *, body: dict | None = None, headers: dict | None = None) -> dict:
    hdrs = {"Host": "abc123.execute-api.us-east-1.amazonaws.com", "Content-Type": "application/json", **(headers or {})}
    return {
        "resource": path, "path": path, "httpMethod": method, "headers": hdrs, "multiValueHeaders": {k: [v] for k, v in hdrs.items()},
        "queryStringParameters": None, "multiValueQueryStringParameters": None, "pathParameters": None, "stageVariables": None,
        "requestContext": {"requestId": "req-test-1", "stage": "dev", "path": f"/dev{path}", "httpMethod": method, "resourcePath": path,
                           "accountId": "123456789012", "apiId": "abc123", "identity": {"sourceIp": "203.0.113.7", "userAgent": "test"}},
        "body": json.dumps(body) if body is not None else None, "isBase64Encoded": False,
    }


def call(method, path, **kw):
    resp = handler(event(method, path, **kw), None)
    try:
        body = json.loads(resp["body"]) if resp.get("body") else None
    except json.JSONDecodeError:  # e.g. the CORS preflight answers plain text
        body = resp["body"]
    return resp["statusCode"], body, resp


@pytest.fixture()
def stack(settings):
    fake_hl, fake_db = FakeHealthLake(page_size=50), FakeDynamoDB()
    client = HealthLakeClient(DATASTORE_ID, credentials=credentials(APP_KEY), transport=fake_hl.transport(), sleep=lambda s: None, max_retries=1)
    repo = HealthLakeFHIRRepository(client, Terminology.load(PACKAGE_DIR), DynamoAppStateStore(TABLE, client=fake_db))
    container = build_container(settings, repository=repo, explainer=MockExplanationService())
    app.dependency_overrides[get_container] = lambda: container
    yield container, fake_hl, fake_db
    app.dependency_overrides.clear()


def dump(model):
    return model.model_dump(mode="json", by_alias=True)


# ---- every documented route ---------------------------------------------------------------------------
def test_every_documented_route_works_through_the_handler(stack):
    assert call("GET", "/v1/health")[:2] == (200, {"status": "ok"})
    status, body, _ = call("GET", "/v1/patients")
    assert status == 200 and [p["id"] for p in body["patients"]] == PATIENTS
    assert call("GET", "/v1/patients/P002/snapshot")[0] == 200
    status, analysis, _ = call("POST", "/v1/patients/P002/analyses")
    assert status == 200 and analysis["analysisId"] == "AN-P002-001" and analysis["aiExplanation"] is None
    assert call("GET", "/v1/patients/P002/analyses/latest")[1]["analysisId"] == "AN-P002-001"
    status, expl, _ = call("POST", "/v1/patients/P002/analyses/AN-P002-001/explanation")
    assert status == 200 and expl["mode"] == "mock"
    assert call("GET", "/v1/patients/P002/analyses/latest")[1]["aiExplanation"]["mode"] == "mock"  # persisted in DynamoDB
    doc_id = call("GET", "/v1/patients/P002/snapshot")[1]["documents"][0]["id"]
    status, doc, _ = call("GET", f"/v1/patients/P002/documents/{doc_id}")
    assert status == 200 and doc["text"].strip()


def test_errors_keep_the_documented_shapes(stack):
    assert call("GET", "/v1/patients/P999/snapshot")[0] == 404
    assert call("POST", "/v1/patients/P999/analyses")[0] == 404
    assert call("POST", "/v1/patients/P001/analyses/AN-P001-009/explanation")[0] == 404
    assert call("GET", "/v1/patients/P001/analyses/latest")[0] == 404  # nothing analysed yet
    assert call("GET", "/v1/patients/P001/documents/nope")[0] == 404


# ---- golden parity through the handler ----------------------------------------------------------------
@pytest.mark.parametrize("pid", PATIENTS)
def test_snapshot_and_analysis_through_the_handler_equal_the_local_backend(stack, container, pid):
    """`container` is the local backend (conftest); `stack` is HealthLake + DynamoDB behind the Lambda handler."""
    assert call("GET", f"/v1/patients/{pid}/snapshot")[1] == dump(container.snapshots.snapshot(pid))
    via_lambda = call("POST", f"/v1/patients/{pid}/analyses")[1]
    local = dump(container.analyses.analyze(pid))
    for volatile in ("generatedAt",):
        via_lambda.pop(volatile), local.pop(volatile)
    assert via_lambda == local


def test_golden_facts_through_the_handler(stack):
    run = lambda pid: call("POST", f"/v1/patients/{pid}/analyses")[1]
    assert run("P001")["overallSeverity"] == "HIGH"
    p6 = run("P006")
    assert [f["ruleId"] for f in p6["findings"]] == ["DDI-003"] and [g["ruleId"] for g in p6["dataGaps"]] == ["DG-001"]
    assert [(f["ruleId"], f["severity"]) for f in run("P008")["findings"]] == [("DL-001", "HIGH"), ("DDI-003", "MODERATE")]
    p9, p10 = run("P009"), run("P010")
    assert (p9["overallSeverity"], p9["findings"], p9["dataGaps"]) == ("NONE", [], [])
    assert p10["overallSeverity"] == "NEEDS_DATA" and call("GET", "/v1/patients/P010/snapshot")[1]["labs"] == []


# ---- read-only against HealthLake, state in DynamoDB -----------------------------------------------------
def test_the_whole_flow_reads_healthlake_only_and_keeps_state_in_dynamodb(stack):
    _, fake_hl, fake_db = stack
    for pid in PATIENTS:
        call("POST", f"/v1/patients/{pid}/analyses")
        call("POST", f"/v1/patients/{pid}/analyses/AN-{pid}-001/explanation")
    assert {r["action"] for r in fake_hl.requests} <= {"ReadResource", "SearchWithGet"}  # the Lambda role's only HealthLake actions
    assert fake_hl.all_current("DetectedIssue") == [] and fake_hl.all_current("RiskAssessment") == []
    assert sum(len(v) for t in fake_hl.store.values() for v in t.values()) == 53  # nothing written, still version 1
    assert set(fake_db.calls) <= {"put_item", "get_item", "update_item", "query"}  # never scan / delete
    assert len([k for k in fake_db.items if k[1].startswith("ANALYSIS#")]) == 10


# ---- failure behaviour ----------------------------------------------------------------------------------
def test_healthlake_failure_is_a_generic_503_that_leaks_no_internals(stack):
    _, fake_hl, _ = stack
    fake_hl.fail_next = [503] * 20
    status, body, resp = call("GET", "/v1/patients/P001/snapshot")
    assert status == 503 and body == {"detail": "Clinical data store temporarily unavailable"}
    text = json.dumps(resp)
    for leak in ("HealthLake", DATASTORE_ID, "healthlake.us-east-1", "Traceback", "Unavailable", "HTTP 5"):
        assert leak not in text, leak


def test_dynamodb_failure_is_a_generic_503_too(stack):
    _, _, fake_db = stack
    fake_db.fail_with = "ProvisionedThroughputExceededException"
    status, body, _ = call("POST", "/v1/patients/P001/analyses")
    assert status == 503 and body == {"detail": "Clinical data store temporarily unavailable"}


# ---- CORS --------------------------------------------------------------------------------------------------
def test_cors_preflight_and_headers_for_the_local_ui_origin(stack):
    status, _, resp = call("OPTIONS", "/v1/patients/P001/analyses", headers={
        "Origin": ORIGIN, "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type"})
    hdrs = {k.lower(): v for k, v in resp["headers"].items()}
    assert status == 200 and hdrs["access-control-allow-origin"] == ORIGIN and "POST" in hdrs["access-control-allow-methods"]
    _, _, resp = call("GET", "/v1/health", headers={"Origin": ORIGIN})
    assert {k.lower(): v for k, v in resp["headers"].items()}["access-control-allow-origin"] == ORIGIN
    _, _, resp = call("GET", "/v1/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in {k.lower() for k in resp["headers"]}


# ---- logging -----------------------------------------------------------------------------------------------
def test_access_line_is_one_json_record_with_ids_route_status_latency_and_nothing_else(stack, caplog):
    caplog.set_level(logging.DEBUG)
    call("POST", "/v1/patients/P001/analyses")
    lines = [json.loads(r.getMessage()) for r in caplog.records if r.name == "app.access"]
    assert lines and set(lines[-1]) == {"method", "route", "status", "ms", "requestId"}
    assert lines[-1] | {"ms": 0} == {"method": "POST", "route": "/v1/patients/{patient_id}/analyses", "status": 200, "ms": 0, "requestId": "req-test-1"}


def test_logs_contain_no_patient_names_note_text_or_credentials(stack, caplog):
    caplog.set_level(logging.DEBUG)
    doc_id = call("GET", "/v1/patients/P002/snapshot")[1]["documents"][0]["id"]
    call("POST", "/v1/patients/P002/analyses")
    call("POST", "/v1/patients/P002/analyses/AN-P002-001/explanation")
    note = call("GET", f"/v1/patients/P002/documents/{doc_id}")[1]["text"]
    name = call("GET", "/v1/patients")[1]["patients"][1]["name"]
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert name not in text and note[:40] not in text
    for secret in ("app-secret-test-value", APP_KEY, "Signature="):
        assert secret not in text


def test_sdk_and_adapter_loggers_are_pinned_so_paths_and_signatures_never_reach_the_logs():
    for name in ("mangum", "botocore", "boto3", "urllib3", "httpx"):
        assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING, name
