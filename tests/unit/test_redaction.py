"""No credential may reach logs or the user-visible fallbackReason, whatever an SDK/HTTP layer says."""
from __future__ import annotations

import logging

import pytest

from app.container import build_container
from app.redact import install_log_redaction, redact
from app.services.explanation.base import ExplanationService
from app.services.explanation.factory import SafeExplanationService
from app.services.explanation.mock import MockExplanationService

FAKE_KEY = "sk-ant-api03-FAKEKEYFORTESTS0123456789abcdef"


class LeakyPrimary(ExplanationService):
    """Simulates an SDK/proxy error whose message echoes credentials."""

    def explain(self, *a, **k):
        raise RuntimeError(f"401 from gateway: x-api-key: {FAKE_KEY}; Authorization: Bearer abcdef1234567890")


def test_redact_removes_key_shapes_and_keeps_normal_text():
    text = f"key {FAKE_KEY} and Bearer abcdef1234567890 and x-api-key: zzzzzzzz1234 ok"
    out = redact(text)
    assert FAKE_KEY not in out and "abcdef1234567890" not in out and "zzzzzzzz1234" not in out
    assert out.startswith("key [REDACTED]") and out.endswith("ok")
    plain = "Invalid API key provided. rate limited after 3 tries; model claude-opus-5 not found"
    assert redact(plain) == plain


def test_redact_removes_the_configured_env_value_even_if_oddly_shaped(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "custom-nonstandard-secret-value")
    assert "custom-nonstandard-secret-value" not in redact("boom custom-nonstandard-secret-value boom")


# ---- multi-provider credential leakage (Anthropic/OpenAI/Gemini/Databricks) -------------------------------------
@pytest.mark.parametrize("env_var,value", [
    ("ANTHROPIC_API_KEY", "sk-ant-api03-EXACTVALUEEXAMPLE0123456789"),
    ("OPENAI_API_KEY", "sk-proj-EXACTVALUEEXAMPLE0123456789abcdefgh"),
    ("GEMINI_API_KEY", "AIzaSyEXACTVALUEEXAMPLE01234567890abcd"),
    ("DATABRICKS_TOKEN", "dapiEXACTVALUEEXAMPLE0123456789abcdef"),
])
def test_redact_removes_the_exact_configured_value_for_every_provider_credential(monkeypatch, env_var, value):
    monkeypatch.setenv(env_var, value)
    text = f"request failed, credential was {value}, please retry"
    out = redact(text)
    assert value not in out and "[REDACTED]" in out


@pytest.mark.parametrize("shaped_secret", [
    "sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",  # OpenAI-shaped key, no env var set
    "AIzaSyABCDEFGHIJKLMNOPQRSTUVWXYZ01234567",  # Google API-key-shaped value (Gemini), no env var set
    "dapi00112233445566778899aabbccddeeff0011",  # Databricks personal-access-token shape, no env var set
])
def test_redact_strips_provider_key_shaped_values_by_pattern_even_without_the_env_var_set(shaped_secret):
    out = redact(f"Incorrect API key provided: {shaped_secret}")
    assert shaped_secret not in out and "[REDACTED]" in out


def test_redact_does_not_confuse_an_anthropic_key_with_the_openai_pattern():
    key = "sk-ant-api03-DONOTLEAKME0123456789abcdef"
    out = redact(f"auth header: {key}")
    assert key not in out and "sk-ant-" not in out


def test_fallback_reason_and_logs_never_contain_the_credential(container, caplog, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)
    snap = container.snapshots.snapshot("P001")
    a = container.analyses.run("P001").model_copy(update={"ai_explanation": None})
    with caplog.at_level(logging.DEBUG):
        ex = SafeExplanationService(LeakyPrimary(), MockExplanationService()).explain(snap, a)
    assert ex.mode == "mock" and ex.fallback_code == "EXPLANATION_UNAVAILABLE"
    assert ex.fallback_reason == "The AI explanation service is unavailable."  # public message only
    assert "RuntimeError" not in ex.fallback_reason
    assert "RuntimeError" in caplog.text  # the raw cause stays in the server log ...
    assert FAKE_KEY not in caplog.text and "abcdef1234567890" not in caplog.text  # ... redacted


def test_analysis_orchestrator_logs_no_credential_when_explainer_raises(settings, caplog):
    with caplog.at_level(logging.DEBUG):
        result = build_container(settings, explainer=LeakyPrimary()).analyses.run("P001")
    assert result.overall_severity == "HIGH" and result.ai_explanation is None
    assert "AI explanation failed" in caplog.text
    assert FAKE_KEY not in caplog.text and "abcdef1234567890" not in caplog.text
    assert "Traceback" not in caplog.text  # tracebacks would carry unredacted exception text


def test_global_record_factory_redacts_any_logger_and_is_idempotent(caplog):
    install_log_redaction()
    install_log_redaction()
    with caplog.at_level(logging.DEBUG):
        # non-SDK loggers: credentials are redacted (SDK/HTTP debug loggers are silenced outright; see test_sdk_logging.py)
        logging.getLogger("uvicorn.error").debug("request headers x-api-key: %s", FAKE_KEY)
        logging.getLogger("some.worker").info("using %s", FAKE_KEY)
    assert FAKE_KEY not in caplog.text
    assert caplog.text.count("[REDACTED]") == 2


@pytest.mark.parametrize("pid", ["P001", "P008", "P010"])
def test_app_code_does_not_log_patient_data_or_note_text(container, caplog, pid):
    with caplog.at_level(logging.DEBUG, logger="app"):
        container.analyses.run(pid)
    assert "Demo" not in caplog.text and "note" not in caplog.text.lower()
