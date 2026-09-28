"""OpenAI Responses API provider (fake client; no network, no key). Same prompt/schema/guard/fallbacks as
the Claude adapter -- only the transport differs. RESPONSE_SCHEMA is sent unmodified (already strict-mode
compliant), so there is no schema-relaxation/re-validation path to test here (unlike Databricks/Gemini)."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.services.explanation import failures
from app.services.explanation.base import ExplanationUnavailable, GroundingViolation
from app.services.explanation.factory import SafeExplanationService, create_explanation_service
from app.services.explanation.mock import MockExplanationService
from app.services.explanation.openai import OpenAIExplanationService
from app.terminology import Terminology
from tests.conftest import PACKAGE_DIR
from tests.unit.test_explanation import payload_from_mock

MODEL = "gpt-5.6-luna"
TERMS = Terminology.load(PACKAGE_DIR)


class FakeOpenAIClient:
    def __init__(self, payload=None, *, status="completed", text=None, refusal=False, incomplete_reason=None):
        self.calls = []
        if refusal:
            content = [SimpleNamespace(type="refusal", refusal="content policy")]
        else:
            content = [SimpleNamespace(type="output_text", text=text if text is not None else json.dumps(payload))]
        incomplete_details = SimpleNamespace(reason=incomplete_reason) if incomplete_reason else None
        self._resp = SimpleNamespace(status=status, model=MODEL, output=[SimpleNamespace(content=content)],
                                     incomplete_details=incomplete_details)
        self.responses = SimpleNamespace(create=self._create)

    def _create(self, **kw):
        self.calls.append(kw)
        return self._resp


def service(client):
    return OpenAIExplanationService(PACKAGE_DIR, TERMS, MODEL, client=client)


def test_grounded_output_is_accepted_and_the_request_uses_strict_json_schema(container):
    payload, _ = payload_from_mock(container, "P008")
    fake = FakeOpenAIClient(payload)
    snap, a = container.snapshots.snapshot("P008"), container.analyses.analyze("P008").model_copy(update={"ai_explanation": None})
    ex = service(fake).explain(snap, a, None)
    assert ex.mode == "llm" and ex.model == MODEL and ex.grounded_in_findings_only is True
    (call,) = fake.calls
    assert call["text"]["format"]["strict"] is True
    from app.services.explanation.claude import RESPONSE_SCHEMA
    assert call["text"]["format"]["schema"] == RESPONSE_SCHEMA  # unmodified, no relaxation


def test_refusal_is_the_public_declined_category(container):
    snap, a = container.snapshots.snapshot("P001"), container.analyses.analyze("P001").model_copy(update={"ai_explanation": None})
    with pytest.raises(ExplanationUnavailable) as exc:
        service(FakeOpenAIClient(refusal=True)).explain(snap, a, None)
    assert failures.classify(exc.value) == failures.DECLINED


def test_max_output_tokens_incomplete_is_the_public_incomplete_category(container):
    snap, a = container.snapshots.snapshot("P001"), container.analyses.analyze("P001").model_copy(update={"ai_explanation": None})
    with pytest.raises(ExplanationUnavailable) as exc:
        service(FakeOpenAIClient(status="incomplete", incomplete_reason="max_output_tokens", text="cut off")).explain(snap, a, None)
    assert failures.classify(exc.value) == failures.INCOMPLETE


def test_bad_json_is_unavailable(container):
    snap, a = container.snapshots.snapshot("P001"), container.analyses.analyze("P001").model_copy(update={"ai_explanation": None})
    with pytest.raises(ExplanationUnavailable):
        service(FakeOpenAIClient(text="not json")).explain(snap, a, None)


def test_guard_still_rejects_ungrounded_output_proving_the_provider_cannot_bypass_it(container):
    payload, _ = payload_from_mock(container, "P001")
    payload["summary"] += " Start warfarin 5 mg daily."
    snap, a = container.snapshots.snapshot("P001"), container.analyses.analyze("P001").model_copy(update={"ai_explanation": None})
    with pytest.raises(GroundingViolation) as exc:
        service(FakeOpenAIClient(payload)).explain(snap, a, None)
    assert exc.value.raw_output and exc.value.model_name == MODEL and failures.classify(exc.value) == failures.REJECTED


# ---- error classification: the openai SDK's own exceptions already duck-type into failures.classify() -----------
class FakeOpenAIError(Exception):
    def __init__(self, status_code):
        super().__init__(f"fake openai error {status_code}")
        self.status_code = status_code


@pytest.mark.parametrize("status,public", [(401, failures.UNAVAILABLE), (404, failures.UNAVAILABLE),
                                           (429, failures.TEMPORARY), (500, failures.TEMPORARY)])
def test_openai_sdk_exceptions_classify_correctly_with_no_custom_wrapping(status, public):
    assert failures.classify(FakeOpenAIError(status)) == public


# ---- wiring --------------------------------------------------------------------------------------------------
def settings_for(tmp_path, **kw):
    base = Settings.from_env()
    return Settings(**{**base.__dict__, "package_dir": PACKAGE_DIR, "output_dir": tmp_path, **kw})


def test_factory_uses_mock_without_an_openai_key_and_builds_the_provider_with_one(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg = settings_for(tmp_path, explanation_mode="auto", explanation_provider="openai", explanation_model=MODEL)
    assert isinstance(create_explanation_service(cfg), MockExplanationService)

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    svc = create_explanation_service(cfg)  # construction never imports the SDK eagerly (only _get_client() does)
    assert isinstance(svc, SafeExplanationService) and isinstance(svc._primary, OpenAIExplanationService)
    assert svc._primary._model == MODEL


def test_real_lazy_import_constructs_a_client_with_a_monkeypatched_key(monkeypatch):
    pytest.importorskip("openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    client = OpenAIExplanationService(PACKAGE_DIR, TERMS, MODEL)._get_client()
    assert client is not None  # no network call made; just proves the lazy `import openai` path works


def test_get_client_passes_the_env_credential_explicitly_not_an_implicit_sdk_lookup(monkeypatch):
    """Regression: _get_client() used to call `openai.OpenAI(timeout=...)` with no api_key at all, relying
    entirely on the SDK's own implicit os.environ lookup -- the same class of bug that made
    `check_provider_availability.py --verify gemini` fail with "No API key was provided" even though the
    credential was genuinely available. This proves OPENAI_API_KEY is read out and passed as an explicit
    kwarg to the constructor."""
    pytest.importorskip("openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-only-set-by-this-test")
    captured = {}

    class FakeOpenAI:
        def __init__(self, **kw):
            captured.update(kw)

    import openai

    monkeypatch.setattr(openai, "OpenAI", FakeOpenAI)
    OpenAIExplanationService(PACKAGE_DIR, TERMS, MODEL)._get_client()
    assert captured["api_key"] == "sk-test-only-set-by-this-test"


def test_settings_from_env_parses_openai_fields(monkeypatch):
    monkeypatch.setenv("OPENAI_TIMEOUT_SECONDS", "7")
    monkeypatch.setenv("OPENAI_MAX_TOKENS", "1234")
    s = Settings.from_env()
    assert s.openai_timeout_seconds == 7.0 and s.openai_max_tokens == 1234


def test_settings_from_env_parses_the_secret_arn(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY_SECRET_ARN", raising=False)
    assert Settings.from_env().openai_api_key_secret_arn is None
    arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:medsafety/openai-abc123"
    monkeypatch.setenv("OPENAI_API_KEY_SECRET_ARN", arn)
    assert Settings.from_env().openai_api_key_secret_arn == arn


# ---- production deployment config: docs/adr/0011 (OpenAI is the initial deployed provider) ------------------------
DEPLOYED_MODEL = "gpt-5.6-luna"
SECRET_ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:medsafety/openai-abc123"
SECRET_VALUE = "sk-proj-FETCHED-FROM-SECRETS-MANAGER-NOT-A-REAL-KEY-000000000"


class FakeSecretsManagerClient:
    def __init__(self, value=SECRET_VALUE):
        self.calls = []
        self._value = value

    def get_secret_value(self, SecretId):
        self.calls.append(SecretId)
        return {"SecretString": self._value}


def test_factory_fetches_from_secrets_manager_before_the_auto_mode_credential_gate(tmp_path, monkeypatch):
    """The exact Lambda scenario: EXPLANATION_PROVIDER=openai, EXPLANATION_MODEL=gpt-5.6-luna,
    OPENAI_API_KEY_SECRET_ARN set, OPENAI_API_KEY itself never set directly (never packaged, never in
    .local/providers.env). Without loading the secret before the auto-mode gate, _has_credentials() would
    see no OPENAI_API_KEY and silently fall back to mock even though a secret IS configured and fetchable."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    import boto3

    client = FakeSecretsManagerClient()
    monkeypatch.setattr(boto3, "client", lambda service_name: client if service_name == "secretsmanager" else None)

    cfg = settings_for(tmp_path, explanation_mode="auto", explanation_provider="openai",
                       explanation_model=DEPLOYED_MODEL, openai_api_key_secret_arn=SECRET_ARN)
    svc = create_explanation_service(cfg)
    assert isinstance(svc, SafeExplanationService) and isinstance(svc._primary, OpenAIExplanationService)
    assert svc._primary._model == DEPLOYED_MODEL
    assert client.calls == [SECRET_ARN]
    assert __import__("os").environ["OPENAI_API_KEY"] == SECRET_VALUE  # populated so redact.py protects it too


def test_explanation_mode_claude_is_provider_agnostic_legacy_naming(tmp_path, monkeypatch):
    """The exact deployed config (infrastructure/aws/api/40_lambda.sh): EXPLANATION_MODE=claude,
    EXPLANATION_PROVIDER=openai. `claude` predates multi-provider support and, despite the name, means "always
    attempt the configured EXPLANATION_PROVIDER, skip the `auto`-mode credential-presence gate, fall back to the
    mock on any failure" (factory.create_explanation_service's docstring) -- it already deploys this way for
    provider=bedrock (docs/PHASE4_API.md). This proves the same holds for provider=openai: the secret is still
    fetched from Secrets Manager before construction, and the resulting service is the real OpenAI provider, not
    a name-based special case."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    import boto3

    client = FakeSecretsManagerClient()
    monkeypatch.setattr(boto3, "client", lambda service_name: client if service_name == "secretsmanager" else None)

    cfg = settings_for(tmp_path, explanation_mode="claude", explanation_provider="openai",
                       explanation_model=DEPLOYED_MODEL, openai_api_key_secret_arn=SECRET_ARN)
    svc = create_explanation_service(cfg)
    assert isinstance(svc, SafeExplanationService) and isinstance(svc._primary, OpenAIExplanationService)
    assert svc._primary._model == DEPLOYED_MODEL
    assert client.calls == [SECRET_ARN]


def test_factory_still_uses_mock_when_neither_a_key_nor_a_secret_arn_is_configured(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY_SECRET_ARN", raising=False)
    cfg = settings_for(tmp_path, explanation_mode="auto", explanation_provider="openai", explanation_model=DEPLOYED_MODEL)
    assert isinstance(create_explanation_service(cfg), MockExplanationService)


def test_factory_degrades_to_mock_never_crashes_when_secrets_manager_denies_access(tmp_path, monkeypatch):
    """AI must remain non-blocking: a Secrets Manager failure (wrong ARN, missing IAM permission, throttling)
    degrades to the deterministic mock exactly like any other provider-construction failure -- it must never
    propagate out of create_explanation_service() and break the app."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    import boto3

    class DeniedClient:
        def get_secret_value(self, SecretId):
            raise RuntimeError("AccessDeniedException: not authorized")

    monkeypatch.setattr(boto3, "client", lambda service_name: DeniedClient() if service_name == "secretsmanager" else None)
    cfg = settings_for(tmp_path, explanation_mode="auto", explanation_provider="openai",
                       explanation_model=DEPLOYED_MODEL, openai_api_key_secret_arn=SECRET_ARN)
    assert isinstance(create_explanation_service(cfg), MockExplanationService)


def test_the_secrets_manager_fetched_key_is_redacted_from_logs_the_same_as_any_other_credential(monkeypatch):
    """Populating os.environ (rather than a separate cache) is what makes this automatic: redact.py's
    exact-value redaction already reads os.getenv("OPENAI_API_KEY") -- no separate redaction path was added
    or needs to be kept in sync for the Secrets-Manager-sourced case."""
    from app.redact import redact
    from app.secrets import load_secret_env

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    import boto3

    monkeypatch.setattr(boto3, "client", lambda service_name: FakeSecretsManagerClient())
    load_secret_env("OPENAI_API_KEY", SECRET_ARN)
    out = redact(f"provider error, saw credential {SECRET_VALUE} in the request")
    assert SECRET_VALUE not in out and "[REDACTED]" in out
