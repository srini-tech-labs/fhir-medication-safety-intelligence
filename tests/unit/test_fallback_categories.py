"""fallbackCode / fallbackReason are a small stable public set; provider detail never reaches the client.

Uses the REAL Anthropic exception classes (401, 404, 429, 5xx, connection, timeout) plus fake-client
truncation / refusal / guard-rejection, driven through the HTTP API exactly as the frontend calls it.
"""
from __future__ import annotations

import json
import logging

import anthropic
import httpx2
import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_container
from app.container import build_container
from app.main import create_app
from app.services.explanation import failures
from app.services.explanation.base import ExplanationService
from app.services.explanation.factory import SafeExplanationService
from app.services.explanation.mock import MockExplanationService
from tests.unit.test_explanation import FakeClient, claude, payload_from_mock

PLANTED_KEY = "sk-ant-api03-PLANTEDKEY0123456789abcdefghij"
PROVIDER_TEXT = "PROVIDER-SECRET-DETAIL for model claude-opus-5 request req_01ABCDEF"
FORBIDDEN = ["PROVIDER-SECRET", "sk-ant", "claude-", "req_0", "authentication_error", "not_found_error", "Error code",
             "Traceback", "APIStatusError", "AuthenticationError", "NotFoundError", "RateLimitError",
             "InternalServerError", "APIConnectionError", "RuntimeError", "status_code", "anthropic", "401", "404"]


def api_error(cls, status: int):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    body = {"type": "error", "error": {"type": "provider_error", "message": PROVIDER_TEXT, "key": PLANTED_KEY},
            "request_id": "req_01ABCDEF"}
    response = httpx2.Response(status, request=request, json=body)
    return cls(f"Error code: {status} - {body}", response=response, body=body)


class Raises(ExplanationService):
    def __init__(self, exc):
        self.exc = exc

    def explain(self, *a, **k):
        raise self.exc


REQ = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
CASES = {
    "401 authentication": (lambda c: Raises(api_error(anthropic.AuthenticationError, 401)), failures.UNAVAILABLE),
    "403 permission": (lambda c: Raises(api_error(anthropic.PermissionDeniedError, 403)), failures.UNAVAILABLE),
    "404 unknown model": (lambda c: Raises(api_error(anthropic.NotFoundError, 404)), failures.UNAVAILABLE),
    "400 bad request": (lambda c: Raises(api_error(anthropic.BadRequestError, 400)), failures.UNAVAILABLE),
    "generic failure": (lambda c: Raises(RuntimeError(f"boom {PROVIDER_TEXT} {PLANTED_KEY}")), failures.UNAVAILABLE),
    "429 rate limit": (lambda c: Raises(api_error(anthropic.RateLimitError, 429)), failures.TEMPORARY),
    "500 provider error": (lambda c: Raises(api_error(anthropic.InternalServerError, 500)), failures.TEMPORARY),
    "connection error": (lambda c: Raises(anthropic.APIConnectionError(request=REQ)), failures.TEMPORARY),
    "timeout": (lambda c: Raises(anthropic.APITimeoutError(request=REQ)), failures.TEMPORARY),
    "truncated output": (lambda c: claude(FakeClient({}, stop_reason="max_tokens")), failures.INCOMPLETE),
    "malformed output": (lambda c: claude(FakeClient(text="not json")), failures.INCOMPLETE),
    "refusal": (lambda c: claude(FakeClient({}, stop_reason="refusal")), failures.DECLINED),
    "guard rejection": (lambda c: claude(FakeClient(_ungrounded(c))), failures.REJECTED),
}


def _ungrounded(container) -> dict:
    payload, _ = payload_from_mock(container, "P001")
    payload["findingExplanations"][0]["explanation"] = "Also consider warfarin, and the dose should be reduced."
    return payload


@pytest.fixture()
def make_client(settings, container):
    def build(primary: ExplanationService):
        c = build_container(settings, explainer=SafeExplanationService(primary, MockExplanationService()))
        app = create_app()
        app.dependency_overrides[get_container] = lambda: c
        return TestClient(app)
    return build


def request_explanation(client, pid="P001"):
    analysis = client.post(f"/v1/patients/{pid}/analyses").json()
    response = client.post(f"/v1/patients/{pid}/analyses/{analysis['analysisId']}/explanation")
    return analysis, response


@pytest.mark.parametrize("name", CASES)
def test_each_failure_yields_only_a_public_category_and_message(name, container, make_client, settings, caplog):
    build_primary, expected_code = CASES[name]
    client = make_client(build_primary(container))
    with caplog.at_level(logging.DEBUG):
        analysis, response = request_explanation(client)

    assert response.status_code == 200  # a model failure degrades; it is not an HTTP error
    ex = response.json()
    assert ex["mode"] == "mock" and ex["model"] is None
    assert ex["fallbackCode"] == expected_code
    assert ex["fallbackReason"] == failures.PUBLIC_MESSAGES[expected_code]
    assert ex["text"] and ex["groundedInFindingsOnly"] is True  # the client still gets the deterministic-text explanation

    leaked = [t for t in FORBIDDEN if t.lower() in json.dumps(ex).lower()]
    assert leaked == [], f"provider detail reached the client: {leaked}"
    assert "warfarin" not in ex["fallbackReason"].lower()  # guard rejection does not echo the model's text

    saved = json.loads((settings.output_dir / "analyses" / "P001" / f"{analysis['analysisId']}.json").read_text())
    assert saved["aiExplanation"]["fallbackReason"] == ex["fallbackReason"]  # saved record is public-only too
    assert not [t for t in FORBIDDEN if t.lower() in json.dumps(saved["aiExplanation"]).lower()]

    assert PLANTED_KEY not in caplog.text and "PLANTEDKEY" not in caplog.text  # credentials never logged
    assert f"code={expected_code}" in caplog.text  # the server log records the category
    latest = client.get("/v1/patients/P001/analyses/latest").json()
    assert {k: v for k, v in latest.items() if k != "aiExplanation"} == \
           {k: v for k, v in analysis.items() if k != "aiExplanation"}  # findings/gaps untouched


@pytest.mark.parametrize("name,logged", [("401 authentication", "AuthenticationError"), ("404 unknown model", "NotFoundError"),
                                         ("generic failure", "RuntimeError")])
def test_raw_cause_is_retained_only_in_the_redacted_server_log(name, logged, container, make_client, caplog):
    client = make_client(CASES[name][0](container))
    with caplog.at_level(logging.WARNING):
        request_explanation(client)
    assert logged in caplog.text  # useful for operators
    assert PLANTED_KEY not in caplog.text and "PLANTEDKEY" not in caplog.text  # but redacted


def test_classification_table_is_exhaustive_and_stable():
    assert set(failures.PUBLIC_MESSAGES) == {"EXPLANATION_UNAVAILABLE", "EXPLANATION_TEMPORARILY_UNAVAILABLE",
                                             "EXPLANATION_INCOMPLETE", "EXPLANATION_DECLINED", "EXPLANATION_REJECTED"}
    for code, message in failures.PUBLIC_MESSAGES.items():
        assert message and message.endswith(".") and not any(t.lower() in message.lower() for t in FORBIDDEN)
    assert failures.classify(ValueError("x")) == failures.UNAVAILABLE
    assert failures.classify(TimeoutError()) == failures.TEMPORARY
    assert failures.classify(ConnectionError()) == failures.TEMPORARY


def test_unexpected_error_in_the_explanation_endpoint_is_a_generic_503(container, settings, caplog):
    c = build_container(settings, explainer=Raises(RuntimeError(f"internal {PROVIDER_TEXT} {PLANTED_KEY}")))
    app = create_app()
    app.dependency_overrides[get_container] = lambda: c
    client = TestClient(app)
    analysis = client.post("/v1/patients/P001/analyses").json()
    with caplog.at_level(logging.DEBUG):
        r = client.post(f"/v1/patients/P001/analyses/{analysis['analysisId']}/explanation")
    assert r.status_code == 503 and r.json() == {"detail": failures.PUBLIC_MESSAGES[failures.UNAVAILABLE]}
    assert not [t for t in FORBIDDEN if t.lower() in r.text.lower()]
    assert PLANTED_KEY not in caplog.text
