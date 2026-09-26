"""Fake-client tests for scripts/diagnose_databricks_400.py -- the three-step diagnostic that isolates the
HTTP 400 databricks-gpt-oss-120b returned on every normal production request in the first live Databricks
run. No real call is made anywhere in this file. Proves: the step sequence stops at the first failure, the
sanitized-error extraction never includes request headers/token/patient data, and a successful step's
summary never includes response content.
"""
from __future__ import annotations

import importlib.util
from types import SimpleNamespace

import pytest

from app.config import REPO_ROOT
from app.terminology import Terminology
from tests.conftest import PACKAGE_DIR

spec = importlib.util.spec_from_file_location("diagnose_databricks_400", REPO_ROOT / "scripts" / "diagnose_databricks_400.py")
diag = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diag)

FAKE_TOKEN = "dapi00112233445566778899aabbccddeeff0011"  # realistic dapi<hex...> shape, not a real token
TERMS = Terminology.load(PACKAGE_DIR)
REAL_SYSTEM_PROMPT = (PACKAGE_DIR / "prompts" / "ai_explanation_system.txt").read_text("utf-8")


def steps_for(endpoint: str, model_input: dict) -> list:
    return diag.build_steps(endpoint, PACKAGE_DIR, TERMS, model_input)


class FakeChatClient:
    """Simulates a sequence of chat.completions.create outcomes: each entry in `outcomes` is either an
    exception instance (raised) or a fake successful response object."""

    def __init__(self, outcomes: list):
        self._outcomes = list(outcomes)
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def fake_response(finish_reason="stop", prompt_tokens=12, completion_tokens=34, content="not inspected by the test"):
    message = SimpleNamespace(content=content)
    choice = SimpleNamespace(finish_reason=finish_reason, message=message)
    usage = SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)
    return SimpleNamespace(choices=[choice], usage=usage)


class FakeBadRequestError(Exception):
    def __init__(self, error_code, message, status_code=400, bearer_header_present_for_the_test_only=None):
        super().__init__(f"Error code: {status_code} - {{'error_code': '{error_code}', 'message': '{message}'}}")
        self.status_code = status_code
        self.body = {"error_code": error_code, "message": message}
        # A real APIStatusError also carries `.request`/`.response` with headers -- this test proves
        # sanitized_error() never reads them, by deliberately NOT setting any such attribute here at all.


# ---- sanitized_error(): never headers/token/patient data, only code/status/message ------------------------------
def test_sanitized_error_extracts_only_code_status_and_message():
    exc = FakeBadRequestError("INVALID_PARAMETER", "reasoning_effort is not supported for this model")
    err = diag.sanitized_error(exc)
    assert err == {"exception_type": "FakeBadRequestError", "http_status": 400,
                   "error_code": "INVALID_PARAMETER", "message": "reasoning_effort is not supported for this model"}


def test_sanitized_error_redacts_a_credential_shaped_value_in_the_message():
    exc = FakeBadRequestError("BAD_REQUEST", f"rejected, saw header with {FAKE_TOKEN}")
    err = diag.sanitized_error(exc)
    assert FAKE_TOKEN not in err["message"] and "[REDACTED]" in err["message"]


def test_sanitized_error_never_reads_request_or_header_attributes():
    """Even if the real exception object carried `.request`/`.response` with headers (as openai's
    APIStatusError does), sanitized_error() must never surface them -- proven by an exception whose only
    other attributes are headers, and asserting the returned dict has exactly the 4 expected keys."""
    exc = FakeBadRequestError("BAD_REQUEST", "generic failure")
    exc.request = SimpleNamespace(headers={"Authorization": f"Bearer {FAKE_TOKEN}"})
    exc.response = SimpleNamespace(headers={"Authorization": f"Bearer {FAKE_TOKEN}"})
    err = diag.sanitized_error(exc)
    assert set(err) == {"exception_type", "http_status", "error_code", "message"}
    assert FAKE_TOKEN not in str(err)


def test_sanitized_error_falls_back_to_str_exc_when_there_is_no_parsed_body():
    exc = Exception("a generic connection failure, no body")
    err = diag.sanitized_error(exc)
    assert err["error_code"] is None and "generic connection failure" in err["message"]


# ---- summarize_success(): only token counts and finish_reason, never response content -----------------------
def test_summarize_success_never_includes_response_content():
    resp = fake_response(content="a real medication-safety explanation naming P008's findings")
    summary = diag.summarize_success(resp)
    assert summary == {"finish_reason": "stop", "prompt_tokens": 12, "completion_tokens": 34}
    assert "P008" not in str(summary) and "medication-safety" not in str(summary)


# ---- run_diagnostic(): stops at the first failure ------------------------------------------------------------
def test_run_diagnostic_stops_at_step_a_on_failure():
    client = FakeChatClient([FakeBadRequestError("INVALID_PARAMETER", "bad request shape"), "unused", "unused"])
    steps = steps_for("databricks-gpt-oss-120b", {"summary": "fake model input"})
    results = diag.run_diagnostic(client, steps)
    assert len(results) == 1 and results[0][1] is False  # stopped after step A, B and C never attempted
    assert len(client.calls) == 1


def test_run_diagnostic_reaches_step_c_only_if_a_and_b_pass():
    client = FakeChatClient([fake_response(), fake_response(), FakeBadRequestError("BAD_REQUEST", "payload rejected")])
    steps = steps_for("databricks-gpt-oss-120b", {"summary": "fake model input"})
    results = diag.run_diagnostic(client, steps)
    assert [ok for _, ok, _ in results] == [True, True, False]
    assert len(client.calls) == 3


def test_run_diagnostic_all_three_pass():
    client = FakeChatClient([fake_response(), fake_response(), fake_response()])
    steps = steps_for("databricks-gpt-oss-120b", {"summary": "fake model input"})
    results = diag.run_diagnostic(client, steps)
    assert [ok for _, ok, _ in results] == [True, True, True]


# ---- build_steps(): matches the exact escalation the user specified -------------------------------------------
def test_build_steps_matches_the_specified_escalation():
    steps = steps_for("databricks-gpt-oss-120b", {"k": "v"})
    assert len(steps) == 3
    (label_a, kw_a), (label_b, kw_b), (label_c, kw_c) = steps

    assert "response_format" not in kw_a and len(kw_a["messages"]) == 1 and kw_a["messages"][0]["role"] == "user"
    assert kw_a["max_tokens"] == 256 and kw_a["reasoning_effort"] == "low" and kw_a["model"] == "databricks-gpt-oss-120b"

    assert kw_b["response_format"] == {"type": "json_object"} and len(kw_b["messages"]) == 1

    assert kw_c["response_format"] == {"type": "json_object"}
    assert [m["role"] for m in kw_c["messages"]] == ["system", "user"]


def test_build_steps_step_c_uses_the_real_fixed_databricks_provider_prompts():
    """Step C must be byte-for-byte what the FIXED DatabricksExplanationService actually sends in
    production, including the json-mention fix in both _system_prompt() and _user_prompt() -- this is what
    makes a future --execute re-run a real verification of the fix, not a re-discovery of the same bug."""
    steps = steps_for("databricks-gpt-oss-120b", {"k": "v"})
    _, _, (_, kw_c) = steps
    system_message, user_message = kw_c["messages"]
    assert system_message["content"].startswith(REAL_SYSTEM_PROMPT)  # the real frozen prompt, unmodified prefix
    assert "json" in system_message["content"].lower()
    assert "json" in user_message["content"].lower()  # the actual finding: this is the message that must say it
    assert '"k": "v"' in user_message["content"]  # the real model input, still JSON-encoded underneath the prefix


def test_dry_run_mode_needs_no_credentials_and_makes_no_network_call(monkeypatch, capsys):
    """--execute is required for any network call; without it, main() must not need DATABRICKS_HOST/TOKEN
    at all and must never construct a real client."""
    import subprocess
    import sys

    env = {k: v for k, v in __import__("os").environ.items() if k not in ("DATABRICKS_HOST", "DATABRICKS_TOKEN")}
    proc = subprocess.run([sys.executable, str(REPO_ROOT / "scripts" / "diagnose_databricks_400.py")],
                          env=env, capture_output=True, text=True, cwd=REPO_ROOT / "backend")
    assert proc.returncode == 0
    assert "DRY RUN" in proc.stdout and "not sent" in proc.stdout
    assert "No DATABRICKS_HOST" not in proc.stdout
