"""Bedrock Runtime Converse provider, Claude `text_format` mode (paused, undeployed; the Nova `tool` mode is in test_nova_explanation.py). Requests are validated against the REAL botocore `bedrock-runtime` service model
(botocore Stubber), so a wrong `outputConfig` / `modelId` / block shape fails offline."""
from __future__ import annotations

import json
import logging

import boto3
import pytest
from botocore.exceptions import ReadTimeoutError
from botocore.stub import ANY, Stubber

from app.config import Settings
from app.services.explanation import failures
from app.services.explanation.base import ExplanationUnavailable, GroundingViolation
from app.services.explanation.bedrock import BedrockClaudeExplanationService
from app.services.explanation.claude import RESPONSE_SCHEMA
from app.services.explanation.factory import SafeExplanationService, create_explanation_service
from app.services.explanation.mock import MockExplanationService
from app.terminology import Terminology
from tests.conftest import PACKAGE_DIR
from tests.unit.test_explanation import payload_from_mock

MODEL = "us.anthropic.claude-sonnet-5"
TERMS = Terminology.load(PACKAGE_DIR)
SYSTEM = (PACKAGE_DIR / "prompts" / "ai_explanation_system.txt").read_text("utf-8")


def real_client():
    return boto3.client("bedrock-runtime", region_name="us-east-1", aws_access_key_id="AKIATESTONLY", aws_secret_access_key="test-secret")


def converse_response(text=None, *, payload=None, stop="end_turn", blocks=None):
    content = blocks if blocks is not None else [{"text": text if text is not None else json.dumps(payload)}]
    return {"output": {"message": {"role": "assistant", "content": content}}, "stopReason": stop,
            "usage": {"inputTokens": 1850, "outputTokens": 310, "totalTokens": 2160}, "metrics": {"latencyMs": 4200}}


EXPECTED = {"modelId": MODEL, "system": [{"text": SYSTEM}], "messages": ANY,
            "inferenceConfig": {"maxTokens": 4000},
            "outputConfig": {"effort": "low", "textFormat": {"type": "json_schema", "structure": {"jsonSchema": {
                "schema": json.dumps(RESPONSE_SCHEMA), "name": "medication_safety_explanation", "description": ANY}}}}}


def service(stubber_client):
    return BedrockClaudeExplanationService(PACKAGE_DIR, TERMS, MODEL, region="us-east-1", client=stubber_client)


def run(container, pid, response=None, error=None):
    payload, _ = payload_from_mock(container, pid)
    client = real_client()
    with Stubber(client) as stub:
        if error:
            stub.add_client_error("converse", service_error_code=error, http_status_code=400)
        else:
            stub.add_response("converse", response or converse_response(payload=payload), EXPECTED)
        svc = service(client)
        snap, analysis = container.snapshots.snapshot(pid), container.analyses.analyze(pid).model_copy(update={"ai_explanation": None})
        try:
            return svc.explain(snap, analysis, None), stub
        finally:
            stub.assert_no_pending_responses()


# ---- request / response shape ------------------------------------------------------------------------
@pytest.mark.parametrize("pid", ["P001", "P008", "P010"])
def test_grounded_output_is_accepted_and_the_request_matches_the_converse_service_model(container, pid):
    explanation, _ = run(container, pid)
    assert explanation.mode == "llm" and explanation.model == MODEL and explanation.grounded_in_findings_only is True


def test_reasoning_blocks_are_ignored_and_text_blocks_are_used(container):
    payload, _ = payload_from_mock(container, "P001")
    blocks = [{"reasoningContent": {"reasoningText": {"text": "thinking...", "signature": "sig"}}}, {"text": json.dumps(payload)}]
    explanation, _ = run(container, "P001", converse_response(blocks=blocks))
    assert explanation.mode == "llm"


def test_the_schema_sent_is_the_one_the_guard_expects():
    schema = json.loads(EXPECTED["outputConfig"]["textFormat"]["structure"]["jsonSchema"]["schema"])
    assert schema == RESPONSE_SCHEMA and schema["additionalProperties"] is False
    # only constructs AWS documents as supported for Converse structured output (no min/max/length constraints, no external $ref)
    text = json.dumps(schema)
    for banned in ("minimum", "maximum", "multipleOf", "minLength", "maxLength", "$ref"):
        assert banned not in text


# ---- stop reasons ------------------------------------------------------------------------------------------
@pytest.mark.parametrize("stop", ["content_filtered", "guardrail_intervened"])
def test_content_filter_stop_reasons_are_the_public_declined_category(container, stop):
    with pytest.raises(ExplanationUnavailable) as exc:
        run(container, "P001", converse_response("", stop=stop))
    assert failures.classify(exc.value) == failures.DECLINED


@pytest.mark.parametrize("stop", ["max_tokens", "malformed_model_output", "model_context_window_exceeded"])
def test_truncated_or_malformed_stop_reasons_are_incomplete(container, stop):
    with pytest.raises(ExplanationUnavailable) as exc:
        run(container, "P001", converse_response("{}", stop=stop))
    assert failures.classify(exc.value) == failures.INCOMPLETE


def test_invalid_json_is_incomplete_and_ungrounded_output_is_rejected_with_the_raw_text_kept(container):
    with pytest.raises(ExplanationUnavailable):
        run(container, "P001", converse_response("not json"))
    payload, _ = payload_from_mock(container, "P001")
    payload["summary"] += " Start warfarin 5 mg daily."
    with pytest.raises(GroundingViolation) as exc:
        run(container, "P001", converse_response(payload=payload))
    assert exc.value.raw_output and exc.value.model_name == MODEL and failures.classify(exc.value) == failures.REJECTED


# ---- provider errors ---------------------------------------------------------------------------------------
@pytest.mark.parametrize("code,public", [("ThrottlingException", failures.TEMPORARY), ("ModelTimeoutException", failures.TEMPORARY),
                                         ("ServiceUnavailableException", failures.TEMPORARY),
                                         ("AccessDeniedException", failures.UNAVAILABLE), ("ResourceNotFoundException", failures.UNAVAILABLE)])
def test_provider_errors_map_to_public_categories(container, code, public):
    with pytest.raises(ExplanationUnavailable) as exc:
        run(container, "P001", error=code)
    assert failures.classify(exc.value) == public


def test_a_rejected_schema_is_reported_not_worked_around(container):
    """ValidationException (e.g. an unsupported schema feature) must surface once -- no silent retry without structured output."""
    with pytest.raises(ExplanationUnavailable) as exc:
        run(container, "P001", error="ValidationException")  # Stubber.assert_no_pending_responses + a single queued call
    assert "ValidationException" in str(exc.value) and failures.classify(exc.value) == failures.UNAVAILABLE


def test_read_timeout_is_temporary(container):
    class Slow:
        def converse(self, **kw):
            raise ReadTimeoutError(endpoint_url="https://bedrock-runtime.us-east-1.amazonaws.com")

    payload, _ = payload_from_mock(container, "P001")
    svc = service(Slow())
    snap, analysis = container.snapshots.snapshot("P001"), container.analyses.analyze("P001").model_copy(update={"ai_explanation": None})
    with pytest.raises(ExplanationUnavailable) as exc:
        svc.explain(snap, analysis, None)
    assert failures.classify(exc.value) == failures.TEMPORARY


def test_safe_service_returns_the_labelled_mock_and_captures_only_model_output(container):
    payload, _ = payload_from_mock(container, "P001")
    payload["summary"] += " Start warfarin 5 mg daily."
    captured = []
    client = real_client()
    with Stubber(client) as stub:
        stub.add_response("converse", converse_response(payload=payload), EXPECTED)
        safe = SafeExplanationService(service(client), MockExplanationService(), on_rejection=captured.append)
        snap, analysis = container.snapshots.snapshot("P001"), container.analyses.analyze("P001").model_copy(update={"ai_explanation": None})
        out = safe.explain(snap, analysis, None)
    assert out.mode == "mock" and out.fallback_code == failures.REJECTED and len(captured) == 1 and captured[0]["model"] == MODEL
    client2 = real_client()
    with Stubber(client2) as stub:
        stub.add_client_error("converse", service_error_code="ThrottlingException", http_status_code=429)
        out = SafeExplanationService(service(client2), MockExplanationService(), on_rejection=captured.append).explain(snap, analysis, None)
    assert out.fallback_code == failures.TEMPORARY and len(captured) == 1  # provider errors have no model output to capture


# ---- logging -----------------------------------------------------------------------------------------------
def test_logs_carry_token_counts_but_no_patient_content_prompts_or_credentials(container, caplog):
    caplog.set_level(logging.DEBUG)
    run(container, "P001")
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "bedrock converse mode=text_format stop=end_turn in=1850 out=310" in text
    for forbidden in ("Lisa", "Potassium", "AKIATESTONLY", "test-secret", "Signature=", SYSTEM[:60]):
        assert forbidden not in text


# ---- wiring ------------------------------------------------------------------------------------------------
def settings_for(tmp_path, **kw):
    base = Settings.from_env()
    return Settings(**{**base.__dict__, "package_dir": PACKAGE_DIR, "output_dir": tmp_path, **kw})


def test_factory_builds_the_bedrock_provider_without_any_anthropic_key(tmp_path, monkeypatch):
    for v in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(v, raising=False)
    for mode in ("claude", "auto"):
        svc = create_explanation_service(settings_for(tmp_path, explanation_mode=mode, explanation_provider="bedrock", explanation_model=MODEL))
        assert isinstance(svc, SafeExplanationService) and isinstance(svc._primary, BedrockClaudeExplanationService)
        assert svc._primary._model == MODEL and svc._primary._region == "us-east-1"


def test_factory_keeps_anthropic_as_the_default_and_mock_mode_never_calls_a_model(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    assert isinstance(create_explanation_service(settings_for(tmp_path, explanation_mode="auto", explanation_provider="anthropic")), MockExplanationService)
    assert isinstance(create_explanation_service(settings_for(tmp_path, explanation_mode="mock", explanation_provider="bedrock")), MockExplanationService)
    with pytest.raises(ValueError, match="EXPLANATION_PROVIDER"):
        create_explanation_service(settings_for(tmp_path, explanation_provider="mantle"))


def test_real_client_is_configured_with_short_timeouts_and_a_single_attempt():
    svc = BedrockClaudeExplanationService(PACKAGE_DIR, TERMS, MODEL, timeout_seconds=20.0)
    client = svc._get_client()
    cfg = client.meta.config
    # a retry would double the wait past API Gateway's 29 s cap: exactly ONE attempt in total
    assert (cfg.connect_timeout, cfg.read_timeout, cfg.retries["total_max_attempts"]) == (3, 20.0, 1)
    assert client.meta.region_name == "us-east-1" and client.meta.service_model.service_name == "bedrock-runtime"


def test_default_mode_follows_the_model_family_and_unknown_families_are_an_error():
    from app.services.explanation.bedrock import default_mode

    assert default_mode("us.anthropic.claude-sonnet-5") == "text_format"
    assert default_mode("us.amazon.nova-2-lite-v1:0") == "tool"
    with pytest.raises(ValueError, match="BEDROCK_STRUCTURED_OUTPUT"):
        default_mode("us.meta.llama4-v1:0")
    with pytest.raises(ValueError):
        BedrockClaudeExplanationService(PACKAGE_DIR, TERMS, "us.meta.llama4-v1:0")
    with pytest.raises(ValueError, match="unsupported BEDROCK_STRUCTURED_OUTPUT"):
        BedrockClaudeExplanationService(PACKAGE_DIR, TERMS, MODEL, structured_output="json_mode")
