"""Databricks Foundation Model API provider (fake client; no network, no key, no ~/.databrickscfg access).
Same prompt/schema/guard/fallbacks as the Claude adapter -- only the transport differs (OpenAI-client-compatible
chat completions). Structured-output support varies by serving model, so the wire schema is never trusted
blindly: the response is always re-validated with schema_check before the guard -- tested here explicitly,
the same defense-in-depth Nova uses."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.services.explanation import failures
from app.services.explanation.base import ExplanationUnavailable, GroundingViolation
from app.services.explanation.databricks import DatabricksExplanationService
from app.services.explanation.factory import SafeExplanationService, create_explanation_service
from app.services.explanation.mock import MockExplanationService
from app.terminology import Terminology
from tests.conftest import PACKAGE_DIR
from tests.unit.test_explanation import payload_from_mock

ENDPOINT = "medsafety-eval-endpoint"
HOST = "https://example-workspace.cloud.databricks.com"
TERMS = Terminology.load(PACKAGE_DIR)


class FakeChatClient:
    def __init__(self, payload=None, *, finish_reason="stop", text=None, model=ENDPOINT):
        self.calls = []
        message = SimpleNamespace(content=text if text is not None else json.dumps(payload))
        choice = SimpleNamespace(finish_reason=finish_reason, message=message)
        self._resp = SimpleNamespace(choices=[choice], model=model)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        return self._resp


def service(client, **kw):
    return DatabricksExplanationService(PACKAGE_DIR, TERMS, ENDPOINT, host=HOST, token="fake-token", client=client, **kw)


def test_grounded_output_is_accepted_and_json_object_mode_is_the_default(container):
    payload, _ = payload_from_mock(container, "P008")
    fake = FakeChatClient(payload)
    snap, a = container.snapshots.snapshot("P008"), container.analyses.analyze("P008").model_copy(update={"ai_explanation": None})
    ex = service(fake).explain(snap, a, None)
    assert ex.mode == "llm" and ex.model == ENDPOINT and ex.grounded_in_findings_only is True
    (call,) = fake.calls
    assert call["model"] == ENDPOINT and call["response_format"] == {"type": "json_object"}


def test_json_object_mode_user_message_contains_the_word_json(container):
    """Regression: the Foundation Model API (OpenAI-API-compatible) rejects response_format=json_object
    unless some message contains the word "json". The frozen system prompt already says "Return concise
    JSON:" -- but that alone was NOT enough for real: scripts/diagnose_databricks_400.py's step C sent
    exactly that system prompt + a plain JSON-dumped user message and still 400'd with "messages must
    contain the word 'json'", while step B (json_object + only a user message saying "as JSON") had
    already passed. So it's the USER message specifically that must say it, not just any message."""
    payload, _ = payload_from_mock(container, "P001")
    fake = FakeChatClient(payload)
    snap, a = container.snapshots.snapshot("P001"), container.analyses.analyze("P001").model_copy(update={"ai_explanation": None})
    service(fake).explain(snap, a, None)
    (call,) = fake.calls
    assert call["response_format"] == {"type": "json_object"}
    user_message = next(m for m in call["messages"] if m["role"] == "user")
    assert "json" in user_message["content"].lower(), "the user message must mention json, not just the system message"


def test_json_object_mode_user_message_still_carries_the_unmodified_model_input_json(container):
    """The added JSON-mention line must not alter the model_input payload itself -- just prefix it."""
    payload, model_input = payload_from_mock(container, "P001")
    fake = FakeChatClient(payload)
    snap, a = container.snapshots.snapshot("P001"), container.analyses.analyze("P001").model_copy(update={"ai_explanation": None})
    service(fake).explain(snap, a, None)
    (call,) = fake.calls
    user_message = next(m for m in call["messages"] if m["role"] == "user")
    assert user_message["content"].endswith(json.dumps(model_input, indent=2))


def test_prompt_only_mode_user_message_is_not_prefixed(container):
    """The json-mention prefix is json_object-mode-only; prompt_only already gets its own JSON instructions
    inlined into the system prompt (schema dump) and does not need the user message touched too."""
    payload, model_input = payload_from_mock(container, "P001")
    fake = FakeChatClient(payload)
    snap, a = container.snapshots.snapshot("P001"), container.analyses.analyze("P001").model_copy(update={"ai_explanation": None})
    service(fake, structured_output="prompt_only").explain(snap, a, None)
    (call,) = fake.calls
    user_message = next(m for m in call["messages"] if m["role"] == "user")
    assert user_message["content"] == json.dumps(model_input, indent=2)


def test_prompt_only_mode_sends_no_response_format_and_inlines_the_schema_in_the_system_prompt(container):
    payload, _ = payload_from_mock(container, "P001")
    fake = FakeChatClient(payload)
    snap, a = container.snapshots.snapshot("P001"), container.analyses.analyze("P001").model_copy(update={"ai_explanation": None})
    service(fake, structured_output="prompt_only").explain(snap, a, None)
    (call,) = fake.calls
    assert "response_format" not in call
    assert "groundedInFindingsOnly" in call["messages"][0]["content"]  # the schema is inlined as text


def test_an_unrecognized_structured_output_mode_is_refused_at_construction():
    with pytest.raises(ValueError, match="DATABRICKS_STRUCTURED_OUTPUT"):
        service(None, structured_output="tool")


def test_content_filter_is_the_public_declined_category(container):
    snap, a = container.snapshots.snapshot("P001"), container.analyses.analyze("P001").model_copy(update={"ai_explanation": None})
    with pytest.raises(ExplanationUnavailable) as exc:
        service(FakeChatClient(finish_reason="content_filter", text="")).explain(snap, a, None)
    assert failures.classify(exc.value) == failures.DECLINED


def test_length_finish_reason_is_the_public_incomplete_category(container):
    snap, a = container.snapshots.snapshot("P001"), container.analyses.analyze("P001").model_copy(update={"ai_explanation": None})
    with pytest.raises(ExplanationUnavailable) as exc:
        service(FakeChatClient(finish_reason="length", text="cut off")).explain(snap, a, None)
    assert failures.classify(exc.value) == failures.INCOMPLETE


# ---- content normalization: choice.message.content may be a list of typed blocks, not just a string --------------
# (some Databricks-served reasoning models return this shape; found via scripts/diagnose_databricks_p001.py --
# production used to do `text = content or ""`, keeping a non-empty list as-is, and json.loads() on a list
# raises TypeError, uncaught, collapsing to the generic EXPLANATION_UNAVAILABLE instead of EXPLANATION_INCOMPLETE)
def _p001(container):
    return (container.snapshots.snapshot("P001"), container.analyses.analyze("P001").model_copy(update={"ai_explanation": None}))


def test_plain_string_content_is_unaffected_legacy_behavior_preserved(container):
    payload, _ = payload_from_mock(container, "P001")
    snap, a = _p001(container)
    ex = service(FakeChatClient(payload)).explain(snap, a, None)  # text=None -> json.dumps(payload), a plain string
    assert ex.mode == "llm"


def test_list_with_one_recognized_text_block_dict_form(container):
    payload, _ = payload_from_mock(container, "P001")
    snap, a = _p001(container)
    content = [{"type": "text", "text": json.dumps(payload)}]
    ex = service(FakeChatClient(text=content)).explain(snap, a, None)
    assert ex.mode == "llm"


def test_list_with_multiple_recognized_text_blocks_concatenated_in_order(container):
    payload, _ = payload_from_mock(container, "P001")
    full = json.dumps(payload)
    mid = len(full) // 2
    content = [{"type": "text", "text": full[:mid]}, {"type": "text", "text": full[mid:]}]
    snap, a = _p001(container)
    ex = service(FakeChatClient(text=content)).explain(snap, a, None)
    assert ex.mode == "llm"  # only valid if the two halves were joined in order, reconstituting valid JSON


def test_sdk_object_form_text_block(container):
    payload, _ = payload_from_mock(container, "P001")
    content = [SimpleNamespace(type="text", text=json.dumps(payload))]
    snap, a = _p001(container)
    ex = service(FakeChatClient(text=content)).explain(snap, a, None)
    assert ex.mode == "llm"


def test_empty_content_list_fails_closed_as_incomplete_not_a_raw_typeerror(container):
    snap, a = _p001(container)
    with pytest.raises(ExplanationUnavailable) as exc:
        service(FakeChatClient(text=[])).explain(snap, a, None)
    assert failures.classify(exc.value) == failures.INCOMPLETE


def test_list_with_only_unsupported_non_text_blocks_fails_closed_as_incomplete(container):
    snap, a = _p001(container)
    content = [{"type": "reasoning", "reasoning": "some internal chain of thought, never extracted"},
              SimpleNamespace(type="tool_call", tool_call={"name": "lookup"})]
    with pytest.raises(ExplanationUnavailable) as exc:
        service(FakeChatClient(text=content)).explain(snap, a, None)
    assert failures.classify(exc.value) == failures.INCOMPLETE


def test_malformed_json_extracted_from_a_text_block_is_incomplete_not_a_raw_typeerror(container):
    snap, a = _p001(container)
    content = [{"type": "text", "text": "not valid json"}]
    with pytest.raises(ExplanationUnavailable) as exc:
        service(FakeChatClient(text=content)).explain(snap, a, None)
    assert failures.classify(exc.value) == failures.INCOMPLETE


def test_valid_json_from_a_text_block_that_fails_response_schema_is_incomplete(container):
    payload, _ = payload_from_mock(container, "P001")
    del payload["groundedInFindingsOnly"]  # a required key missing -> schema_check.validate() must catch it
    content = [{"type": "text", "text": json.dumps(payload)}]
    snap, a = _p001(container)
    with pytest.raises(ExplanationUnavailable) as exc:
        service(FakeChatClient(text=content)).explain(snap, a, None)
    assert failures.classify(exc.value) == failures.INCOMPLETE and exc.value.raw_output


def test_valid_schema_from_a_text_block_that_then_fails_the_grounding_guard_is_rejected(container):
    payload, _ = payload_from_mock(container, "P001")
    payload["summary"] += " Start warfarin 5 mg daily."  # schema-valid, but ungrounded treatment language
    content = [{"type": "text", "text": json.dumps(payload)}]
    snap, a = _p001(container)
    with pytest.raises(GroundingViolation) as exc:
        service(FakeChatClient(text=content)).explain(snap, a, None)
    assert failures.classify(exc.value) == failures.REJECTED  # correctly reaches the (unchanged) guard, not INCOMPLETE


# ---- mandatory post-response schema_check re-validation (json_object only guarantees valid JSON) -----------------
def test_output_missing_a_required_key_is_caught_by_schema_check_before_the_guard(container):
    payload, _ = payload_from_mock(container, "P001")
    del payload["groundedInFindingsOnly"]
    snap, a = container.snapshots.snapshot("P001"), container.analyses.analyze("P001").model_copy(update={"ai_explanation": None})
    with pytest.raises(ExplanationUnavailable) as exc:
        service(FakeChatClient(payload)).explain(snap, a, None)
    assert failures.classify(exc.value) == failures.INCOMPLETE and exc.value.raw_output


def test_output_with_an_unexpected_extra_key_is_rejected(container):
    payload, _ = payload_from_mock(container, "P001")
    payload["riskScore"] = 0.9
    snap, a = container.snapshots.snapshot("P001"), container.analyses.analyze("P001").model_copy(update={"ai_explanation": None})
    with pytest.raises(ExplanationUnavailable) as exc:
        service(FakeChatClient(payload)).explain(snap, a, None)
    assert failures.classify(exc.value) == failures.INCOMPLETE


def test_bad_json_is_unavailable(container):
    snap, a = container.snapshots.snapshot("P001"), container.analyses.analyze("P001").model_copy(update={"ai_explanation": None})
    with pytest.raises(ExplanationUnavailable):
        service(FakeChatClient(text="not json")).explain(snap, a, None)


def test_guard_still_rejects_ungrounded_output_proving_the_provider_cannot_bypass_it(container):
    payload, _ = payload_from_mock(container, "P001")
    payload["summary"] += " Start warfarin 5 mg daily."
    snap, a = container.snapshots.snapshot("P001"), container.analyses.analyze("P001").model_copy(update={"ai_explanation": None})
    with pytest.raises(GroundingViolation) as exc:
        service(FakeChatClient(payload)).explain(snap, a, None)
    assert exc.value.raw_output and exc.value.model_name == ENDPOINT and failures.classify(exc.value) == failures.REJECTED


# ---- error classification: reuses the openai SDK's own duck-typed exceptions, same as openai.py ------------------
class FakeOpenAICompatibleError(Exception):
    def __init__(self, status_code):
        super().__init__(f"fake error {status_code}")
        self.status_code = status_code


@pytest.mark.parametrize("status,public", [(401, failures.UNAVAILABLE), (429, failures.TEMPORARY), (500, failures.TEMPORARY)])
def test_openai_compatible_sdk_exceptions_classify_correctly_with_no_custom_wrapping(status, public):
    assert failures.classify(FakeOpenAICompatibleError(status)) == public


# ---- wiring: Databricks needs BOTH host and token (all-of, unlike the any-of providers) ---------------------------
def settings_for(tmp_path, **kw):
    base = Settings.from_env()
    return Settings(**{**base.__dict__, "package_dir": PACKAGE_DIR, "output_dir": tmp_path, **kw})


@pytest.mark.parametrize("host,token", [(None, None), ("https://x.databricks.com", None), (None, "tok")])
def test_factory_uses_mock_unless_both_host_and_token_are_present(tmp_path, monkeypatch, host, token):
    monkeypatch.delenv("DATABRICKS_HOST", raising=False)
    monkeypatch.delenv("DATABRICKS_TOKEN", raising=False)
    if host:
        monkeypatch.setenv("DATABRICKS_HOST", host)
    if token:
        monkeypatch.setenv("DATABRICKS_TOKEN", token)
    cfg = settings_for(tmp_path, explanation_mode="auto", explanation_provider="databricks",
                       databricks_endpoint=ENDPOINT)
    assert isinstance(create_explanation_service(cfg), MockExplanationService)


def test_factory_builds_the_provider_once_both_host_and_token_are_present(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABRICKS_HOST", HOST)
    monkeypatch.setenv("DATABRICKS_TOKEN", "tok")
    cfg = settings_for(tmp_path, explanation_mode="auto", explanation_provider="databricks",
                       databricks_endpoint=ENDPOINT)
    svc = create_explanation_service(cfg)  # construction never imports the SDK eagerly (only _get_client() does)
    assert isinstance(svc, SafeExplanationService) and isinstance(svc._primary, DatabricksExplanationService)
    assert svc._primary._model == ENDPOINT and svc._primary._host == HOST


def test_real_lazy_import_constructs_a_client_with_a_monkeypatched_token():
    pytest.importorskip("openai")
    client = DatabricksExplanationService(PACKAGE_DIR, TERMS, ENDPOINT, host=HOST, token="fake-token")._get_client()
    assert client is not None  # no network call made; just proves the lazy `import openai` path works


def test_settings_from_env_parses_databricks_fields(monkeypatch):
    monkeypatch.setenv("DATABRICKS_ENDPOINT", ENDPOINT)
    monkeypatch.setenv("DATABRICKS_TIMEOUT_SECONDS", "11")
    monkeypatch.setenv("DATABRICKS_MAX_TOKENS", "3456")
    monkeypatch.setenv("DATABRICKS_STRUCTURED_OUTPUT", "prompt_only")
    s = Settings.from_env()
    assert s.databricks_endpoint == ENDPOINT and s.databricks_timeout_seconds == 11.0
    assert s.databricks_max_tokens == 3456 and s.databricks_structured_output == "prompt_only"
