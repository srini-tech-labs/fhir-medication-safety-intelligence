"""A rejected model output is captured (exact raw text + category + triggering sentence) for human review.

Motivated by the rare live P010 "claims the regimen is safe" rejection whose sentence was not recorded. Capture changes NO
behaviour: the user still gets the labelled mock fallback; the raw output goes to the local diagnostic store only -- never to
logs, never to API clients.
"""
from __future__ import annotations

import json
import logging

import anthropic
import httpx2
import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_container
from app.config import Settings
from app.container import build_container
from app.main import create_app
from app.services.explanation.base import GroundingViolation
from app.services.explanation.factory import SafeExplanationService, create_explanation_service
from app.services.explanation.guard import build_model_input, validate_grounding
from app.services.explanation.mock import MockExplanationService
from tests.unit.test_explanation import TERMS, FakeClient, analysis_and_snapshot, claude, payload_from_mock

MARKER = "The regimen is clinically safe (marker-alpha)."
KEY = "sk-ant-api03-CAPTURETESTSECRET0123456789abcdef"


def run_capture(container, pid, payload=None, client=None):
    """Explain `pid` through Claude adapter -> Safe wrapper with a capturing sink. Returns (records, explanation, fake, analysis)."""
    fake = client or FakeClient(payload)
    records: list[dict] = []
    snap, analysis = analysis_and_snapshot(container, pid)
    safe = SafeExplanationService(claude(fake), MockExplanationService(), on_rejection=records.append)
    return records, safe.explain(snap, analysis), fake, analysis


def unsafe_payload(container, pid="P010"):
    payload, _ = payload_from_mock(container, pid)
    payload["summary"] += " " + MARKER
    return payload


# ---- the concrete review case: a "claims the regimen is safe" rejection ---------------------------------------------
def test_a_safe_claim_rejection_is_captured_with_the_exact_output_and_sentence(container):
    payload = unsafe_payload(container)
    records, explanation, fake, analysis = run_capture(container, "P010", payload)

    assert explanation.mode == "mock" and explanation.fallback_code == "EXPLANATION_REJECTED"  # behaviour unchanged
    (rec,) = records
    assert rec["rawOutput"] == fake._resp.content[0].text            # byte-for-byte what the model returned
    assert json.loads(rec["rawOutput"]) == payload and rec["rawOutputChars"] == len(rec["rawOutput"])
    assert rec["code"] == "EXPLANATION_REJECTED" and rec["category"] == "claims the regimen is safe"
    assert rec["flagged"] == [{"category": "claims the regimen is safe", "sentence": MARKER}]
    assert (rec["patientId"], rec["analysisId"], rec["model"]) == ("P010", analysis.analysis_id, "claude-opus-5")
    assert rec["capturedAt"]


def test_content_categories_capture_the_triggering_sentences(container):
    payload, _ = payload_from_mock(container, "P001")
    payload["findingExplanations"][0]["explanation"] += " Lisinopril should be stopped. This confirms a diagnosis of hyperkalemia."
    records, *_ = run_capture(container, "P001", payload)
    (rec,) = records
    assert {(f["category"], f["sentence"]) for f in rec["flagged"]} >= {
        ("treatment/dosing language", "Lisinopril should be stopped."),
        ("diagnosis language", "This confirms a diagnosis of hyperkalemia."),
    }


def test_disclaimers_do_not_show_up_as_flagged_sentences(container):
    payload, _ = payload_from_mock(container, "P001")
    payload["findingExplanations"][0]["explanation"] += " No treatment guidance is provided. " + MARKER
    records, *_ = run_capture(container, "P001", payload)
    assert [f["category"] for f in records[0]["flagged"]] == ["claims the regimen is safe"]


# ---- other model-completion failures are captured too; provider errors have nothing to capture -----------------------
@pytest.mark.parametrize("client,code,raw", [
    (FakeClient({}, stop_reason="max_tokens", text='{"summary": "cut off mid'), "EXPLANATION_INCOMPLETE", '{"summary": "cut off mid'),
    (FakeClient(text="not json at all"), "EXPLANATION_INCOMPLETE", "not json at all"),
    (FakeClient(text="", stop_reason="refusal"), "EXPLANATION_DECLINED", ""),
])
def test_truncated_malformed_and_refused_completions_are_captured(container, client, code, raw):
    records, explanation, *_ = run_capture(container, "P001", client=client)
    assert explanation.fallback_code == code
    assert [(r["code"], r["rawOutput"]) for r in records] == [(code, raw)]


def test_provider_errors_are_not_captured(container):
    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")

    class Boom:
        messages = type("M", (), {"create": staticmethod(lambda **kw: (_ for _ in ()).throw(anthropic.APIConnectionError(request=req)))})()

    records, explanation, *_ = run_capture(container, "P001", client=Boom())
    assert explanation.fallback_code == "EXPLANATION_TEMPORARILY_UNAVAILABLE" and records == []


def test_a_successful_explanation_captures_nothing(container):
    payload, _ = payload_from_mock(container, "P001")
    records, explanation, *_ = run_capture(container, "P001", payload)
    assert explanation.mode == "llm" and records == []


# ---- the raw output stays in the diagnostic store: not in logs, not in API responses ----------------------------------
def make_client(settings, primary):
    c = build_container(settings, explainer=None)
    sink = c.repository.save_rejected_explanation
    c = build_container(settings, repository=c.repository, explainer=SafeExplanationService(primary, MockExplanationService(), on_rejection=sink))
    app = create_app()
    app.dependency_overrides[get_container] = lambda: c
    return TestClient(app), c


def test_end_to_end_capture_lands_in_the_store_but_never_in_logs_or_the_response(container, settings, caplog):
    payload = unsafe_payload(container)
    client, _ = make_client(settings, claude(FakeClient(payload)))
    analysis = client.post("/v1/patients/P010/analyses").json()
    with caplog.at_level(logging.DEBUG):
        response = client.post(f"/v1/patients/P010/analyses/{analysis['analysisId']}/explanation")

    assert response.status_code == 200 and response.json()["fallbackCode"] == "EXPLANATION_REJECTED"
    (path,) = list((settings.output_dir / "rejected_explanations").glob(f"{analysis['analysisId']}-*.json"))
    stored = json.loads(path.read_text())
    assert json.loads(stored["rawOutput"]) == payload and stored["flagged"][0]["sentence"] == MARKER

    body = response.text
    assert "marker-alpha" not in body and "rawOutput" not in body and "flagged" not in body and "claims the regimen" not in body
    assert "marker-alpha" not in caplog.text and "rawOutput" not in caplog.text       # raw text is never logged
    assert "AI explanation fell back" in caplog.text and "code=EXPLANATION_REJECTED" in caplog.text  # category still is


def test_capture_is_redacted_for_credentials(container):
    payload = unsafe_payload(container)
    payload["summary"] += f" Key echo: {KEY}"
    records, *_ = run_capture(container, "P010", payload)
    assert KEY not in json.dumps(records) and "[REDACTED]" in records[0]["rawOutput"]


def test_a_failing_sink_cannot_break_the_fallback(container, caplog):
    def broken(_record):
        raise OSError("disk full")

    snap, analysis = analysis_and_snapshot(container, "P010")
    safe = SafeExplanationService(claude(FakeClient(unsafe_payload(container))), MockExplanationService(), on_rejection=broken)
    with caplog.at_level(logging.WARNING):
        explanation = safe.explain(snap, analysis)
    assert explanation.mode == "mock" and explanation.fallback_code == "EXPLANATION_REJECTED"
    assert "could not capture rejected explanation: OSError" in caplog.text


# ---- wiring and the off switch ----------------------------------------------------------------------------------
def test_factory_wires_the_repository_sink_and_honours_the_off_switch(settings, container, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")
    settings = Settings(**{**settings.__dict__, "explanation_mode": "claude"})   # the fixture defaults to mock mode
    on = create_explanation_service(settings, container.repository)
    assert isinstance(on, SafeExplanationService) and on._on_rejection == container.repository.save_rejected_explanation
    off = create_explanation_service(Settings(**{**settings.__dict__, "capture_rejected_explanations": False}), container.repository)
    assert off._on_rejection is None
    assert create_explanation_service(settings)._on_rejection is None          # no repository -> nothing to write to


@pytest.mark.parametrize("value,expected", [(None, True), ("true", True), ("1", True), ("false", False), ("0", False), ("off", False), ("NO", False)])
def test_capture_setting_parsing(monkeypatch, value, expected):
    monkeypatch.delenv("CAPTURE_REJECTED_EXPLANATIONS", raising=False)
    if value is not None:
        monkeypatch.setenv("CAPTURE_REJECTED_EXPLANATIONS", value)
    assert Settings.from_env().capture_rejected_explanations is expected


def test_disabled_capture_writes_nothing(container, settings):
    snap, analysis = analysis_and_snapshot(container, "P010")
    safe = SafeExplanationService(claude(FakeClient(unsafe_payload(container))), MockExplanationService(), on_rejection=None)
    assert safe.explain(snap, analysis).fallback_code == "EXPLANATION_REJECTED"
    assert not (settings.output_dir / "rejected_explanations").exists()


# ---- guard change is informational only: rejection messages are unchanged --------------------------------------------
def test_flagged_sentences_do_not_change_the_rejection_message(container):
    payload, model_input = payload_from_mock(container, "P001")
    payload["summary"] += " " + MARKER
    with pytest.raises(GroundingViolation) as exc:
        validate_grounding(payload, model_input, TERMS)
    assert str(exc.value) == "claims the regimen is safe" and exc.value.flagged[0]["sentence"] == MARKER
