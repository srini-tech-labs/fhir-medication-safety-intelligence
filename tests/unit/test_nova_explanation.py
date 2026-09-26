"""Amazon Nova 2 Lite through Bedrock Runtime Converse: the explanation schema as ONE forced tool. Requests are validated against the
REAL botocore `bedrock-runtime` service model (Stubber), so a wrong toolConfig/toolChoice/additionalModelRequestFields shape fails offline.
The contract schema is re-applied to `toolUse.input` before the (unchanged) grounding guard."""
from __future__ import annotations

import copy
import json
import logging

import boto3
import pytest
from botocore.exceptions import ReadTimeoutError
from botocore.stub import ANY, Stubber

from app.config import Settings
from app.services.explanation import failures
from app.services.explanation.base import ExplanationUnavailable, GroundingViolation
from app.services.explanation.bedrock import NOVA_TOOL_SCHEMA, TOOL_NAME, BedrockClaudeExplanationService
from app.services.explanation.factory import SafeExplanationService, create_explanation_service
from app.services.explanation.mock import MockExplanationService
from app.terminology import Terminology
from tests.conftest import PACKAGE_DIR
from tests.unit.test_explanation import payload_from_mock

MODEL = "us.amazon.nova-2-lite-v1:0"
TERMS = Terminology.load(PACKAGE_DIR)
SYSTEM = (PACKAGE_DIR / "prompts" / "ai_explanation_system.txt").read_text("utf-8")

EXPECTED = {
    "modelId": MODEL, "system": [{"text": SYSTEM}], "messages": ANY,
    "inferenceConfig": {"maxTokens": 4000, "temperature": 0.00001},
    "toolConfig": {"tools": [{"toolSpec": {"name": TOOL_NAME, "description": ANY, "inputSchema": {"json": NOVA_TOOL_SCHEMA}}}],
                   "toolChoice": {"tool": {"name": TOOL_NAME}}},
    "additionalModelRequestFields": {"reasoningConfig": {"type": "disabled"}},
}  # exact: no outputConfig, no topP, reasoning explicitly disabled


def real_client():
    return boto3.client("bedrock-runtime", region_name="us-east-1", aws_access_key_id="AKIATESTONLY", aws_secret_access_key="test-secret")


def nova_response(payload=None, *, stop="tool_use", blocks=None, name=TOOL_NAME):
    content = blocks if blocks is not None else [{"toolUse": {"toolUseId": "tooluse_1", "name": name, "input": payload}}]
    return {"output": {"message": {"role": "assistant", "content": content}}, "stopReason": stop,
            "usage": {"inputTokens": 1850, "outputTokens": 380, "totalTokens": 2230}, "metrics": {"latencyMs": 2100}}


def service(client, **kw):
    return BedrockClaudeExplanationService(PACKAGE_DIR, TERMS, MODEL, region="us-east-1", client=client, **kw)


def run(container, pid, response=None, *, error=None, payload_mutator=None, expected=EXPECTED):
    payload, _ = payload_from_mock(container, pid)
    if payload_mutator:
        payload = payload_mutator(copy.deepcopy(payload))
    client = real_client()
    with Stubber(client) as stub:
        if error:
            stub.add_client_error("converse", service_error_code=error, http_status_code=400)
        else:
            stub.add_response("converse", response or nova_response(payload), expected)
        snap, analysis = container.snapshots.snapshot(pid), container.analyses.analyze(pid).model_copy(update={"ai_explanation": None})
        try:
            return service(client).explain(snap, analysis, None)
        finally:
            stub.assert_no_pending_responses()


# ---- request shape ---------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("pid", ["P001", "P006", "P008", "P010"])
def test_grounded_tool_input_is_accepted_and_the_request_matches_the_converse_service_model(container, pid):
    explanation = run(container, pid)
    assert explanation.mode == "llm" and explanation.model == MODEL and explanation.grounded_in_findings_only is True


def test_the_request_is_exactly_the_documented_forced_tool_pattern(container):
    svc = service(None)
    payload, model_input = payload_from_mock(container, "P001")
    req = svc._request(model_input)
    assert set(req) == {"modelId", "system", "messages", "inferenceConfig", "toolConfig", "additionalModelRequestFields"}
    assert "outputConfig" not in req  # that is the Claude path
    assert req["toolConfig"]["toolChoice"] == {"tool": {"name": "record_explanation"}} and len(req["toolConfig"]["tools"]) == 1
    assert req["additionalModelRequestFields"] == {"reasoningConfig": {"type": "disabled"}}
    cfg = req["inferenceConfig"]
    assert cfg["maxTokens"] <= 5000 and cfg["temperature"] >= 0.00001 and "topP" not in cfg and "topK" not in json.dumps(req)
    assert json.loads(req["messages"][0]["content"][0]["text"]) == json.loads(json.dumps(model_input))  # same input the Claude path sends


def test_reasoning_is_never_enabled_and_there_is_no_reasoning_parameter_anywhere_else(container):
    req = service(None)._request(payload_from_mock(container, "P001")[1])
    text = json.dumps(req)
    assert '"enabled"' not in text and "maxReasoningEffort" not in text


def test_a_max_tokens_above_the_documented_nova_limit_is_refused_at_start_up():
    with pytest.raises(ValueError, match="documented Nova limit"):
        service(None, max_tokens=8000)


# ---- response handling -------------------------------------------------------------------------------------------------
def test_extra_text_and_reasoning_blocks_are_ignored(container):
    payload, _ = payload_from_mock(container, "P001")
    blocks = [{"reasoningContent": {"reasoningText": {"text": "[REDACTED]"}}}, {"text": "chatter that must never be shown"},
              {"toolUse": {"toolUseId": "t", "name": TOOL_NAME, "input": payload}}]
    explanation = run(container, "P001", nova_response(blocks=blocks))
    assert explanation.mode == "llm" and "chatter" not in explanation.text


@pytest.mark.parametrize("response", [
    nova_response(blocks=[{"text": "I will just answer in prose."}], stop="end_turn"),                        # text-only although the tool was forced
    nova_response({"summary": "x"}, name="some_other_tool"),                                                  # wrong tool name
    nova_response(blocks=[], stop="tool_use"),                                                                # tool_use but no block
    nova_response(blocks=[{"text": "{"}], stop="malformed_tool_use"), nova_response(blocks=[], stop="malformed_model_output"),
    nova_response(blocks=[{"text": "cut off"}], stop="max_tokens"),
])
def test_missing_wrong_or_malformed_tool_use_is_the_public_incomplete_category(container, response):
    with pytest.raises(ExplanationUnavailable) as exc:
        run(container, "P001", response)
    assert failures.classify(exc.value) == failures.INCOMPLETE


def test_content_filtered_is_the_public_declined_category(container):
    with pytest.raises(ExplanationUnavailable) as exc:
        run(container, "P001", nova_response(blocks=[], stop="content_filtered"))
    assert failures.classify(exc.value) == failures.DECLINED


# ---- the contract schema is re-applied before the guard ---------------------------------------------------------------
@pytest.mark.parametrize("name,mutate", [
    ("extra top-level key", lambda p: {**p, "riskScore": 0.9}),
    ("extra key inside a finding", lambda p: {**p, "findingExplanations": [{**e, "severity": "HIGH"} for e in p["findingExplanations"]] or [{"ruleId": "X", "explanation": "y", "severity": "HIGH"}]}),
    ("missing required key", lambda p: {k: v for k, v in p.items() if k != "groundedInFindingsOnly"}),
    ("wrong type", lambda p: {**p, "summary": 42}),
    ("nullable field wrong type", lambda p: {**p, "dataGapExplanation": ["a"]}),
])
def test_output_the_generation_schema_would_allow_but_the_contract_forbids_is_rejected_and_captured(container, name, mutate):
    """The Nova generation schema drops additionalProperties; the ORIGINAL RESPONSE_SCHEMA still rejects these, before the guard runs."""
    captured = []
    payload, _ = payload_from_mock(container, "P001")
    client = real_client()
    with Stubber(client) as stub:
        stub.add_response("converse", nova_response(mutate(copy.deepcopy(payload))), EXPECTED)
        safe = SafeExplanationService(service(client), MockExplanationService(), on_rejection=captured.append)
        snap, analysis = container.snapshots.snapshot("P001"), container.analyses.analyze("P001").model_copy(update={"ai_explanation": None})
        out = safe.explain(snap, analysis, None)
    assert out.mode == "mock" and out.fallback_code == failures.INCOMPLETE, name
    assert len(captured) == 1 and captured[0]["model"] == MODEL and captured[0]["rawOutputChars"] > 0  # raw output kept for review


def test_schema_valid_but_ungrounded_output_is_still_stopped_by_the_unchanged_guard(container):
    def add_advice(p):
        p["summary"] += " Start warfarin 5 mg daily."
        return p

    with pytest.raises(GroundingViolation) as exc:
        run(container, "P001", payload_mutator=add_advice)
    assert failures.classify(exc.value) == failures.REJECTED and exc.value.raw_output and exc.value.model_name == MODEL


def test_the_guard_behaves_identically_for_nova_and_claude_payloads(container):
    """Same payload -> same verdict regardless of provider (the guard and _finish are shared, unchanged)."""
    from app.services.explanation.bedrock import BedrockClaudeExplanationService as Svc
    from tests.unit.test_bedrock_explanation import EXPECTED as CLAUDE_EXPECTED, converse_response

    payload, _ = payload_from_mock(container, "P008")
    payload["findingExplanations"][0]["explanation"] += " Increase the dose to 10 mg."
    verdicts = []
    for svc_model, response, expected in ((MODEL, nova_response(payload), EXPECTED),
                                          ("us.anthropic.claude-sonnet-5", converse_response(payload=payload), CLAUDE_EXPECTED)):
        client = real_client()
        with Stubber(client) as stub:
            stub.add_response("converse", response, expected)
            snap, analysis = container.snapshots.snapshot("P008"), container.analyses.analyze("P008").model_copy(update={"ai_explanation": None})
            with pytest.raises(GroundingViolation) as exc:
                Svc(PACKAGE_DIR, TERMS, svc_model, client=client).explain(snap, analysis, None)
            verdicts.append((failures.classify(exc.value), sorted(f["category"] for f in exc.value.flagged)))
    assert verdicts[0] == verdicts[1] and verdicts[0][0] == failures.REJECTED


# ---- provider errors: reported once, never worked around -------------------------------------------------------------------
@pytest.mark.parametrize("code,public", [("ThrottlingException", failures.TEMPORARY), ("ModelTimeoutException", failures.TEMPORARY),
                                         ("ServiceUnavailableException", failures.TEMPORARY), ("AccessDeniedException", failures.UNAVAILABLE),
                                         ("ResourceNotFoundException", failures.UNAVAILABLE), ("ValidationException", failures.UNAVAILABLE)])
def test_provider_errors_map_to_public_categories_with_exactly_one_call(container, code, public):
    with pytest.raises(ExplanationUnavailable) as exc:
        run(container, "P001", error=code)  # Stubber: one queued call; a retry or a second mode would fail assert_no_pending/unexpected call
    assert failures.classify(exc.value) == public and code in str(exc.value)


def test_read_timeout_is_temporary(container):
    class Slow:
        def converse(self, **kw):
            raise ReadTimeoutError(endpoint_url="https://bedrock-runtime.us-east-1.amazonaws.com")

    snap, analysis = container.snapshots.snapshot("P001"), container.analyses.analyze("P001").model_copy(update={"ai_explanation": None})
    with pytest.raises(ExplanationUnavailable) as exc:
        service(Slow()).explain(snap, analysis, None)
    assert failures.classify(exc.value) == failures.TEMPORARY


# ---- logging + wiring --------------------------------------------------------------------------------------------------
def test_logs_carry_mode_and_token_counts_only(container, caplog):
    caplog.set_level(logging.DEBUG)
    run(container, "P001")
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "bedrock converse mode=tool stop=tool_use in=1850 out=380" in text
    for forbidden in ("Lisa", "Potassium", "AKIATESTONLY", "test-secret", "Signature=", SYSTEM[:60], "record_explanation"):
        assert forbidden not in text


def test_factory_builds_the_nova_tool_provider_from_settings(tmp_path, monkeypatch):
    for v in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(v, raising=False)
    base = Settings.from_env()
    cfg = Settings(**{**base.__dict__, "package_dir": PACKAGE_DIR, "output_dir": tmp_path, "explanation_mode": "claude", "explanation_provider": "bedrock",
                      "explanation_model": MODEL, "bedrock_max_tokens": 4000, "bedrock_structured_output": None})
    svc = create_explanation_service(cfg)
    assert isinstance(svc, SafeExplanationService) and svc._primary._mode == "tool" and svc._primary._model == MODEL
    assert create_explanation_service(Settings(**{**cfg.__dict__, "bedrock_structured_output": "text_format"}))._primary._mode == "text_format"
