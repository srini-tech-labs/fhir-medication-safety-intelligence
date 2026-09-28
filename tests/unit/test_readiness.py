"""GET /v1/ready: readiness, separate from GET /v1/health liveness.

Contract: 200 {"status":"ready","checks":{"clinicalStore":"ok"|"unavailable","appState":"ok"|"unavailable",
"explanationProvider":"configured"|"not_configured"}}; 503 with "status":"not_ready" when clinicalStore or appState
is unavailable. Never leaks an ARN, hostname, table/datastore id or exception text. Never invokes the AI model.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.api.deps import get_container
from app.container import build_container
from app.main import app
from app.models.contract import AIExplanation, Analysis, Snapshot
from app.repository.app_state_dynamodb import DynamoAppStateStore
from app.repository.healthlake import HealthLakeFHIRRepository
from app.repository.healthlake_client import HealthLakeClient
from app.services.explanation.base import ExplanationService
from app.services.explanation.factory import SafeExplanationService
from app.services.explanation.mock import MockExplanationService
from app.terminology import Terminology
from tests.conftest import PACKAGE_DIR
from tests.support.fake_dynamodb import TABLE, FakeDynamoDB
from tests.support.fake_healthlake import APP_KEY, DATASTORE_ID, FakeHealthLake, credentials

client = TestClient(app)


class RefusingExplanationService(ExplanationService):
    """A primary provider that fails the test if it is ever actually invoked (proves /ready never calls the model)."""

    def explain(self, patient_snapshot: Snapshot, deterministic_analysis: Analysis, note_context: str | None = None) -> AIExplanation:
        raise AssertionError("/v1/ready must never invoke the explanation provider")


def _repo(*, datastore_id: str = DATASTORE_ID, fake_hl: FakeHealthLake | None = None, fake_db: FakeDynamoDB | None = None):
    fake_hl = fake_hl or FakeHealthLake(page_size=50)
    fake_db = fake_db or FakeDynamoDB()
    hl_client = HealthLakeClient(datastore_id, credentials=credentials(APP_KEY), transport=fake_hl.transport(), sleep=lambda s: None, max_retries=1)
    return HealthLakeFHIRRepository(hl_client, Terminology.load(PACKAGE_DIR), DynamoAppStateStore(TABLE, client=fake_db)), fake_hl, fake_db


def _use(settings, repository, explainer):
    container = build_container(settings, repository=repository, explainer=explainer)
    app.dependency_overrides[get_container] = lambda: container
    return container


def teardown_function(_):
    app.dependency_overrides.clear()


def test_ready_is_200_when_everything_is_reachable_and_an_llm_provider_is_wired(settings):
    repo, _, _ = _repo()
    safe = SafeExplanationService(RefusingExplanationService(), MockExplanationService())
    _use(settings, repo, safe)
    resp = client.get("/v1/ready")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ready", "checks": {"clinicalStore": "ok", "appState": "ok", "explanationProvider": "configured"}}


def test_ready_is_503_when_the_clinical_store_is_unreachable(settings):
    repo, _, _ = _repo(datastore_id="wrong-datastore-id-not-the-one-the-fake-serves")  # -> 404 "unknown datastore"
    _use(settings, repo, MockExplanationService())
    resp = client.get("/v1/ready")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["clinicalStore"] == "unavailable"
    assert body["checks"]["appState"] == "ok"  # independent: one dependency down does not hide another's status


def test_ready_is_503_when_application_state_is_unreachable(settings):
    repo, _, fake_db = _repo()
    fake_db.fail_with = "ProvisionedThroughputExceededException"
    _use(settings, repo, MockExplanationService())
    resp = client.get("/v1/ready")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "not_ready"
    assert body["checks"] == {"clinicalStore": "ok", "appState": "unavailable", "explanationProvider": "not_configured"}


def test_ready_reports_explanation_provider_not_configured_without_failing_readiness(settings):
    """A bare mock (no credentials / EXPLANATION_MODE=mock) is a valid, working deployment state -- the deterministic
    path is fully functional without it, so it must not make the instance unready."""
    repo, _, _ = _repo()
    _use(settings, repo, MockExplanationService())
    resp = client.get("/v1/ready")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ready"
    assert resp.json()["checks"]["explanationProvider"] == "not_configured"


def test_ready_never_invokes_the_explanation_provider(settings):
    """Direct proof, not just an inference from the status code: the wired primary raises if called at all."""
    repo, _, _ = _repo()
    safe = SafeExplanationService(RefusingExplanationService(), MockExplanationService())
    _use(settings, repo, safe)
    resp = client.get("/v1/ready")  # would raise AssertionError inside the app (-> 500) if it ever called .explain()
    assert resp.status_code == 200


def test_ready_never_leaks_an_arn_hostname_datastore_id_or_exception_text(settings):
    repo, _, fake_db = _repo(datastore_id="wrong-datastore-id-not-the-one-the-fake-serves")
    fake_db.fail_with = "InternalServerError"
    _use(settings, repo, MockExplanationService())
    resp = client.get("/v1/ready")
    text = resp.text
    for leak in ("arn:aws", DATASTORE_ID, "wrong-datastore-id-not-the-one-the-fake-serves", TABLE, "HealthLakeError",
                 "ClientError", "InternalServerError", "Traceback", "execute-api"):
        assert leak not in text, leak
    assert set(resp.json()["checks"]) == {"clinicalStore", "appState", "explanationProvider"}
    assert set(resp.json()["checks"].values()) <= {"ok", "unavailable", "configured", "not_configured"}


def test_health_liveness_is_unaffected_by_a_broken_clinical_store(settings):
    """Liveness must not be coupled to a dependency: a HealthLake/DynamoDB outage should surface on /ready, and
    an orchestrator must not restart an otherwise-healthy process over it."""
    repo, _, fake_db = _repo(datastore_id="wrong-datastore-id-not-the-one-the-fake-serves")
    fake_db.fail_with = "InternalServerError"
    _use(settings, repo, MockExplanationService())
    assert client.get("/v1/health").json() == {"status": "ok"}
    assert client.get("/v1/health").status_code == 200


def test_ready_with_the_real_openai_provider_wired_never_refetches_the_secret_or_calls_openai(settings, monkeypatch):
    """The exact deployment scenario (docs/adr/0011): EXPLANATION_PROVIDER=openai, a Secrets Manager ARN
    configured, OPENAI_API_KEY not set directly. The secret fetch happens once, at construction time (cold
    start) -- exactly like create_explanation_service() is called once per Lambda execution environment, not
    per request. This proves /v1/ready reports "configured" from that already-built explainer and never
    triggers a SECOND fetch or calls OpenAI itself: after construction, Secrets Manager is swapped for a
    client that raises if ever touched again, then /v1/ready is called."""
    import boto3

    from app.config import Settings
    from app.services.explanation.factory import create_explanation_service
    from app.services.explanation.openai import OpenAIExplanationService

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    class FakeSecretsManagerClient:
        def get_secret_value(self, SecretId):
            return {"SecretString": "sk-proj-FAKE-NOT-A-REAL-KEY-0000000000"}

    monkeypatch.setattr(boto3, "client",
                        lambda service_name: FakeSecretsManagerClient() if service_name == "secretsmanager" else None)

    repo, _, _ = _repo()
    openai_settings = Settings(**{**settings.__dict__, "explanation_mode": "auto", "explanation_provider": "openai",
                                 "explanation_model": "gpt-5.6-luna",
                                 "openai_api_key_secret_arn": "arn:aws:secretsmanager:us-east-1:123456789012:secret:medsafety/openai-abc"})
    explainer = create_explanation_service(openai_settings, repository=repo)  # the one-time, cold-start fetch
    assert isinstance(explainer, SafeExplanationService) and isinstance(explainer._primary, OpenAIExplanationService)

    class SecretsManagerMustNeverBeCalledAgain:
        def get_secret_value(self, SecretId):
            raise AssertionError("/v1/ready must never re-fetch the secret after construction")

    monkeypatch.setattr(boto3, "client",
                        lambda service_name: SecretsManagerMustNeverBeCalledAgain() if service_name == "secretsmanager" else None)

    _use(openai_settings, repo, explainer)
    resp = client.get("/v1/ready")  # would raise (-> 500) if it ever re-fetched the secret or called the model
    assert resp.status_code == 200
    assert resp.json()["checks"]["explanationProvider"] == "configured"


def test_ready_checks_are_independent_of_local_backend_which_is_always_ready(settings):
    """The local (non-HealthLake, non-DynamoDB) backend has no external dependency: readiness is trivially true."""
    from app.repository.factory import create_repository

    local_settings = settings  # the `settings` fixture already wires data_backend="local"
    _use(local_settings, create_repository(local_settings), MockExplanationService())
    resp = client.get("/v1/ready")
    assert resp.status_code == 200
    assert resp.json()["checks"]["clinicalStore"] == "ok" and resp.json()["checks"]["appState"] == "ok"
