"""Google Gemini provider (fake client; no network, no key). Same prompt/schema/guard/fallbacks as the
Claude adapter -- only the transport differs. RESPONSE_SCHEMA is sent to Gemini UNMODIFIED via
response_json_schema (no Gemini-specific derivation exists), but the response is still re-validated with
schema_check before the guard, the same defense-in-depth Databricks/Nova use -- tested here explicitly."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.services.explanation import failures
from app.services.explanation.base import ExplanationUnavailable, GroundingViolation
from app.services.explanation.claude import RESPONSE_SCHEMA
from app.services.explanation.factory import SafeExplanationService, create_explanation_service
from app.services.explanation.gemini import GeminiExplanationService
from app.services.explanation.mock import MockExplanationService
from app.terminology import Terminology
from tests.conftest import PACKAGE_DIR
from tests.unit.test_explanation import payload_from_mock

MODEL = "gemini-3.8-flash"
TERMS = Terminology.load(PACKAGE_DIR)


class FakeGeminiClient:
    def __init__(self, payload=None, *, finish_name="STOP", text=None):
        self.calls = []
        candidate = SimpleNamespace(finish_reason=SimpleNamespace(name=finish_name))
        self._resp = SimpleNamespace(candidates=[candidate], model_version=MODEL,
                                     text=text if text is not None else json.dumps(payload))
        self.models = SimpleNamespace(generate_content=self._generate)

    def _generate(self, **kw):
        self.calls.append(kw)
        return self._resp


class RaisingGeminiClient:
    def __init__(self, exc):
        self._exc = exc
        self.models = SimpleNamespace(generate_content=self._raise)

    def _raise(self, **kw):
        raise self._exc


def service(client):
    return GeminiExplanationService(PACKAGE_DIR, TERMS, MODEL, client=client)


def test_grounded_output_is_accepted_and_response_json_schema_is_unmodified(container):
    payload, _ = payload_from_mock(container, "P008")
    fake = FakeGeminiClient(payload)
    snap, a = container.snapshots.snapshot("P008"), container.analyses.analyze("P008").model_copy(update={"ai_explanation": None})
    ex = service(fake).explain(snap, a, None)
    assert ex.mode == "llm" and ex.model == MODEL and ex.grounded_in_findings_only is True
    (call,) = fake.calls
    assert call["config"]["response_json_schema"] == RESPONSE_SCHEMA  # verbatim, no derivation/weakening
    assert "response_schema" not in call["config"]  # the restricted/legacy field is never used


def test_safety_or_recitation_block_is_the_public_declined_category(container):
    snap, a = container.snapshots.snapshot("P001"), container.analyses.analyze("P001").model_copy(update={"ai_explanation": None})
    for finish in ("SAFETY", "RECITATION"):
        with pytest.raises(ExplanationUnavailable) as exc:
            service(FakeGeminiClient(finish_name=finish, text="")).explain(snap, a, None)
        assert failures.classify(exc.value) == failures.DECLINED


def test_max_tokens_is_the_public_incomplete_category(container):
    snap, a = container.snapshots.snapshot("P001"), container.analyses.analyze("P001").model_copy(update={"ai_explanation": None})
    with pytest.raises(ExplanationUnavailable) as exc:
        service(FakeGeminiClient(finish_name="MAX_TOKENS", text="cut off")).explain(snap, a, None)
    assert failures.classify(exc.value) == failures.INCOMPLETE


# ---- mandatory post-response schema_check re-validation, even though the wire schema was unmodified --------------
def test_output_missing_a_required_key_is_caught_by_schema_check_before_the_guard(container):
    payload, _ = payload_from_mock(container, "P001")
    del payload["groundedInFindingsOnly"]
    snap, a = container.snapshots.snapshot("P001"), container.analyses.analyze("P001").model_copy(update={"ai_explanation": None})
    with pytest.raises(ExplanationUnavailable) as exc:
        service(FakeGeminiClient(payload)).explain(snap, a, None)
    assert failures.classify(exc.value) == failures.INCOMPLETE and exc.value.raw_output


def test_output_with_an_unexpected_extra_key_is_rejected(container):
    payload, _ = payload_from_mock(container, "P001")
    payload["riskScore"] = 0.9
    snap, a = container.snapshots.snapshot("P001"), container.analyses.analyze("P001").model_copy(update={"ai_explanation": None})
    with pytest.raises(ExplanationUnavailable) as exc:
        service(FakeGeminiClient(payload)).explain(snap, a, None)
    assert failures.classify(exc.value) == failures.INCOMPLETE


def test_bad_json_is_unavailable(container):
    snap, a = container.snapshots.snapshot("P001"), container.analyses.analyze("P001").model_copy(update={"ai_explanation": None})
    with pytest.raises(ExplanationUnavailable):
        service(FakeGeminiClient(text="not json")).explain(snap, a, None)


def test_guard_still_rejects_ungrounded_output_proving_the_provider_cannot_bypass_it(container):
    payload, _ = payload_from_mock(container, "P001")
    payload["summary"] += " Start warfarin 5 mg daily."
    snap, a = container.snapshots.snapshot("P001"), container.analyses.analyze("P001").model_copy(update={"ai_explanation": None})
    with pytest.raises(GroundingViolation) as exc:
        service(FakeGeminiClient(payload)).explain(snap, a, None)
    assert exc.value.raw_output and exc.value.model_name == MODEL and failures.classify(exc.value) == failures.REJECTED


# ---- transport/API error classification (a small wrapper, unlike OpenAI's zero-wrapping) --------------------------
class FakeGeminiError(Exception):
    def __init__(self, status_code=None):
        super().__init__("fake gemini error")
        if status_code is not None:
            self.status_code = status_code


@pytest.mark.parametrize("status,public", [(401, failures.UNAVAILABLE), (429, failures.TEMPORARY), (500, failures.TEMPORARY)])
def test_status_code_errors_are_classified_correctly(container, status, public):
    snap, a = container.snapshots.snapshot("P001"), container.analyses.analyze("P001").model_copy(update={"ai_explanation": None})
    with pytest.raises(ExplanationUnavailable) as exc:
        service(RaisingGeminiClient(FakeGeminiError(status_code=status))).explain(snap, a, None)
    assert failures.classify(exc.value) == public


def test_a_schema_rejection_error_is_reported_not_silently_worked_around(container):
    """If the SDK/API rejects the schema itself, the exact (redacted) error text is kept -- never caught and retried
    with an altered schema, because no schema-altering code path exists anywhere in this provider."""
    snap, a = container.snapshots.snapshot("P001"), container.analyses.analyze("P001").model_copy(update={"ai_explanation": None})
    bad_schema_error = FakeGeminiError(status_code=400)
    bad_schema_error.args = ("response_json_schema: unsupported keyword 'patternProperties'",)
    with pytest.raises(ExplanationUnavailable) as exc:
        service(RaisingGeminiClient(bad_schema_error)).explain(snap, a, None)
    assert "patternProperties" in str(exc.value) and failures.classify(exc.value) == failures.UNAVAILABLE


# ---- wiring --------------------------------------------------------------------------------------------------
def settings_for(tmp_path, **kw):
    base = Settings.from_env()
    return Settings(**{**base.__dict__, "package_dir": PACKAGE_DIR, "output_dir": tmp_path, **kw})


def test_factory_uses_mock_without_a_gemini_key_and_builds_the_provider_with_one(tmp_path, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    cfg = settings_for(tmp_path, explanation_mode="auto", explanation_provider="gemini", explanation_model=MODEL)
    assert isinstance(create_explanation_service(cfg), MockExplanationService)

    monkeypatch.setenv("GEMINI_API_KEY", "fake-not-real")
    svc = create_explanation_service(cfg)  # construction never imports the SDK eagerly (only _get_client() does)
    assert isinstance(svc, SafeExplanationService) and isinstance(svc._primary, GeminiExplanationService)
    assert svc._primary._model == MODEL


def test_real_lazy_import_constructs_a_client_with_a_monkeypatched_key(monkeypatch):
    pytest.importorskip("google.genai")
    monkeypatch.setenv("GEMINI_API_KEY", "fake-not-real")
    client = GeminiExplanationService(PACKAGE_DIR, TERMS, MODEL)._get_client()
    assert client is not None  # no network call made; just proves the lazy `from google import genai` path works


def test_settings_from_env_parses_gemini_fields(monkeypatch):
    monkeypatch.setenv("GEMINI_TIMEOUT_SECONDS", "9")
    monkeypatch.setenv("GEMINI_MAX_TOKENS", "2345")
    s = Settings.from_env()
    assert s.gemini_timeout_seconds == 9.0 and s.gemini_max_tokens == 2345
