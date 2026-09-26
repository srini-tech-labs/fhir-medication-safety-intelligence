"""Fake-client tests for scripts/diagnose_databricks_p001.py -- the single-patient (P001) diagnostic that
pinpoints the exact post-generation failure stage. No real call is made anywhere in this file. Proves: each
stage's yes/no is reported correctly, the report never contains the full generated text or full model
input, schema/guard failures are sanitized to path/type/category info, and the final-exception cross-check
(replaying the cached response through the real, unmodified DatabricksExplanationService) reports the
correct exception class and public failure code for each scenario.
"""
from __future__ import annotations

import importlib.util
import json
from types import SimpleNamespace

import pytest

from app.config import REPO_ROOT, Settings
from app.terminology import Terminology
from tests.conftest import PACKAGE_DIR

spec = importlib.util.spec_from_file_location("diagnose_databricks_p001", REPO_ROOT / "scripts" / "diagnose_databricks_p001.py")
diag = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diag)

TERMS = Terminology.load(PACKAGE_DIR)


def settings_for(tmp_path) -> Settings:
    base = Settings.from_env()
    return Settings(**{**base.__dict__, "package_dir": PACKAGE_DIR, "output_dir": tmp_path,
                       "databricks_endpoint": "databricks-gpt-oss-120b", "databricks_max_tokens": 4000})


def fake_response(content: str, finish_reason="stop"):
    message = SimpleNamespace(content=content)
    choice = SimpleNamespace(finish_reason=finish_reason, message=message)
    return SimpleNamespace(choices=[choice], model="gpt-oss-120b-080525")


def golden_payload(container) -> dict:
    from app.services.explanation.mock import MockExplanationService

    a = container.analyses.run("P001")
    ex = MockExplanationService().explain(container.snapshots.snapshot("P001"), a, None)
    return {"summary": ex.summary,
           "findingExplanations": [{"ruleId": e.rule_id, "explanation": e.explanation} for e in ex.finding_explanations],
           "dataGapExplanation": ex.data_gap_explanation, "groundedInFindingsOnly": True}


def patch_client(monkeypatch, response_or_error):
    import openai

    class FakeOpenAI:
        def __init__(self, **kw):
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

        def _create(self, **kw):
            if isinstance(response_or_error, Exception):
                raise response_or_error
            return response_or_error

    monkeypatch.setattr(openai, "OpenAI", FakeOpenAI)


def test_full_success_path(tmp_path, monkeypatch, container):
    patch_client(monkeypatch, fake_response(json.dumps(golden_payload(container))))
    report = diag.diagnose(settings_for(tmp_path), TERMS)
    assert report["http_model_completion_succeeded"] is True
    assert report["response_content_present"] is True
    assert report["json_parse_succeeded"] is True
    assert set(report["parsed_top_level_keys"]) == {"summary", "findingExplanations", "dataGapExplanation", "groundedInFindingsOnly"}
    assert report["schema_check_succeeded"] is True
    assert report["grounding_guard_reached"] is True
    assert report["grounding_guard_verdict"] == "accepted"
    assert report["final_exception_class"] is None and report["final_failure_code"] is None
    assert report["final_mode"] == "llm"


def test_http_call_itself_fails(tmp_path, monkeypatch):
    patch_client(monkeypatch, RuntimeError("connection refused"))
    report = diag.diagnose(settings_for(tmp_path), TERMS)
    assert report["http_model_completion_succeeded"] is False
    assert "connection refused" in report["completion_error"]
    assert "response_content_present" not in report  # stages after the failed call are never reached


def test_response_content_missing(tmp_path, monkeypatch):
    patch_client(monkeypatch, fake_response(""))
    report = diag.diagnose(settings_for(tmp_path), TERMS)
    assert report["http_model_completion_succeeded"] is True
    assert report["response_content_present"] is False
    assert report["json_parse_succeeded"] is False  # empty string is not valid JSON


def test_content_as_a_list_of_one_recognized_text_block_is_now_extracted_not_a_raw_typeerror(tmp_path, monkeypatch):
    """This is the EXACT real shape found by the first real P001 diagnostic run and now fixed:
    choice.message.content as a list of one recognized text block. Before the fix, production's
    `text = content or ""` kept the raw list, and json.loads(list) raised an uncaught TypeError, collapsing
    to the generic EXPLANATION_UNAVAILABLE. Now, DatabricksExplanationService._extract_text() (reused here,
    not reimplemented) correctly extracts the text; since that extracted text happens not to be valid JSON
    in this scenario, it fails cleanly at the JSON-parse stage instead -- json.JSONDecodeError, classified
    EXPLANATION_INCOMPLETE, never a raw TypeError anywhere in the report."""
    patch_client(monkeypatch, fake_response([{"type": "text", "text": "some reasoning-model content shape"}]))
    report = diag.diagnose(settings_for(tmp_path), TERMS)
    assert report["http_model_completion_succeeded"] is True
    assert report["response_content_python_type"] == "list"
    assert report["json_parse_succeeded"] is False
    assert "TypeError" not in report["json_parse_error"]  # normalization succeeded; this is a plain JSON-parse failure
    assert report["final_exception_class"] == "ExplanationUnavailable"
    assert report["final_failure_code"] == "EXPLANATION_INCOMPLETE"


def test_content_as_a_list_with_no_recognized_text_blocks_fails_closed_not_a_raw_typeerror(tmp_path, monkeypatch):
    """A list shape that _extract_text() genuinely cannot handle (no recognized text blocks) must still
    fail closed via ExplanationUnavailable/EXPLANATION_INCOMPLETE, never a raw TypeError -- proving the
    fix's fail-closed guarantee holds for the diagnostic path too, not just the golden-shaped case above."""
    patch_client(monkeypatch, fake_response([{"type": "reasoning", "reasoning": "internal chain of thought"}]))
    report = diag.diagnose(settings_for(tmp_path), TERMS)
    assert report["http_model_completion_succeeded"] is True
    assert report["response_content_python_type"] == "list"
    assert report["json_parse_succeeded"] is False
    assert "TypeError" not in report["json_parse_error"]
    assert "ExplanationUnavailable" in report["json_parse_error"]
    assert report["final_exception_class"] == "ExplanationUnavailable"
    assert report["final_failure_code"] == "EXPLANATION_INCOMPLETE"


def test_json_parse_fails(tmp_path, monkeypatch):
    patch_client(monkeypatch, fake_response("not json at all"))
    report = diag.diagnose(settings_for(tmp_path), TERMS)
    assert report["response_content_present"] is True
    assert report["json_parse_succeeded"] is False
    assert report["parsed_top_level_keys"] is None
    assert report["schema_check_succeeded"] is None  # never reached
    assert report["grounding_guard_reached"] is False
    assert report["final_exception_class"] == "ExplanationUnavailable"
    assert report["final_failure_code"] == "EXPLANATION_INCOMPLETE"  # the exact distinction being verified


def test_schema_check_fails_missing_required_key(tmp_path, monkeypatch, container):
    payload = golden_payload(container)
    del payload["groundedInFindingsOnly"]
    patch_client(monkeypatch, fake_response(json.dumps(payload)))
    report = diag.diagnose(settings_for(tmp_path), TERMS)
    assert report["json_parse_succeeded"] is True
    assert report["schema_check_succeeded"] is False
    assert report["schema_check_problems"] and "groundedInFindingsOnly" in report["schema_check_problems"][0]
    assert report["grounding_guard_reached"] is False  # correctly never reached after a schema failure
    assert report["final_exception_class"] == "ExplanationUnavailable"
    assert report["final_failure_code"] == "EXPLANATION_INCOMPLETE", (
        "a schema-validation failure must classify as EXPLANATION_INCOMPLETE, not the generic "
        "EXPLANATION_UNAVAILABLE -- this is the exact distinction the diagnostic verifies")


def test_schema_check_fails_extra_key(tmp_path, monkeypatch, container):
    payload = golden_payload(container)
    payload["riskScore"] = 0.9
    patch_client(monkeypatch, fake_response(json.dumps(payload)))
    report = diag.diagnose(settings_for(tmp_path), TERMS)
    assert report["schema_check_succeeded"] is False
    assert report["final_failure_code"] == "EXPLANATION_INCOMPLETE"


def test_grounding_guard_rejects(tmp_path, monkeypatch, container):
    payload = golden_payload(container)
    payload["summary"] += " Start warfarin 5 mg daily."
    patch_client(monkeypatch, fake_response(json.dumps(payload)))
    report = diag.diagnose(settings_for(tmp_path), TERMS)
    assert report["schema_check_succeeded"] is True
    assert report["grounding_guard_reached"] is True
    assert report["grounding_guard_verdict"] == "rejected"
    assert "warfarin" not in report["grounding_guard_reason"].lower() or "treatment" in report["grounding_guard_reason"].lower()
    assert report["final_exception_class"] == "GroundingViolation"
    assert report["final_failure_code"] == "EXPLANATION_REJECTED"


def test_report_never_contains_the_full_generated_text_or_full_model_input(tmp_path, monkeypatch, container):
    payload = golden_payload(container)
    full_text = json.dumps(payload)
    patch_client(monkeypatch, fake_response(full_text))
    report = diag.diagnose(settings_for(tmp_path), TERMS)
    blob = json.dumps(report)
    assert payload["summary"] not in blob  # the actual explanation text never appears verbatim
    assert "patientId" not in blob and "structuredData" not in blob  # model_input field names never appear


def test_dry_run_needs_no_credentials_and_makes_no_network_call():
    import subprocess
    import sys

    env = {k: v for k, v in __import__("os").environ.items() if k not in ("DATABRICKS_HOST", "DATABRICKS_TOKEN")}
    proc = subprocess.run([sys.executable, str(REPO_ROOT / "scripts" / "diagnose_databricks_p001.py")],
                          env=env, capture_output=True, text=True, cwd=REPO_ROOT / "backend")
    assert proc.returncode == 0
    assert "DRY RUN" in proc.stdout
    assert "No DATABRICKS_HOST" not in proc.stdout
