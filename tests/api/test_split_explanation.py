"""Deterministic analysis and AI explanation are separate requests.

  POST /v1/patients/{id}/analyses                            -> deterministic result, no model call, no wait
  POST /v1/patients/{id}/analyses/{analysisId}/explanation   -> explanation of the SAVED analysis, requested separately
"""
from __future__ import annotations

import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_container
from app.container import build_container
from app.main import create_app
from app.services.explanation.base import ExplanationService
from app.services.explanation.factory import SafeExplanationService, create_explanation_service
from app.services.explanation.mock import MockExplanationService

DETERMINISTIC_KEYS = ("analysisId", "patientId", "status", "overallSeverity", "summary", "findings", "dataGaps",
                      "riskAssessment", "asOfDate", "generatedAt", "rulesVersion")


def deterministic(analysis: dict) -> dict:
    return {k: analysis[k] for k in DETERMINISTIC_KEYS}


def client_for(settings, explainer) -> TestClient:
    container = build_container(settings, explainer=explainer)
    app = create_app()
    app.dependency_overrides[get_container] = lambda: container
    return TestClient(app)


def saved(settings, pid, aid) -> dict:
    return json.loads((settings.output_dir / "analyses" / pid / f"{aid}.json").read_text())


class Tripwire(ExplanationService):
    def __init__(self):
        self.calls = 0

    def explain(self, *a, **k):
        self.calls += 1
        raise AssertionError("the explainer must not be called by the deterministic endpoint")


class Blocking(ExplanationService):
    """Simulates a hung/slow model: waits until released."""

    def __init__(self):
        self.entered, self.release, self.calls = threading.Event(), threading.Event(), 0

    def explain(self, snapshot, analysis, note_context=None):
        self.calls += 1
        self.entered.set()
        assert self.release.wait(timeout=30), "test never released the explainer"
        return MockExplanationService().explain(snapshot, analysis, note_context)


# ---- analysis is independent of Claude --------------------------------------------------------------------
def test_analysis_succeeds_without_ever_calling_the_explainer(settings):
    tripwire = Tripwire()
    client = client_for(settings, tripwire)
    r = client.post("/v1/patients/P008/analyses")
    assert r.status_code == 200
    body = r.json()
    assert body["overallSeverity"] == "HIGH" and [f["ruleId"] for f in body["findings"]] == ["DL-001", "DDI-003"]
    assert body["aiExplanation"] is None
    assert tripwire.calls == 0
    assert client.get("/v1/patients/P008/analyses/latest").json()["aiExplanation"] is None


@pytest.mark.parametrize("pid", [f"P{n:03d}" for n in range(1, 11)])
def test_all_golden_patients_analyze_with_no_explainer_and_no_key(pid, settings, monkeypatch):
    """Whole path with no key at all: default factory -> mock; analysis then explanation both work."""
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    assert isinstance(create_explanation_service(settings), MockExplanationService)
    container = build_container(settings)  # no injected explainer: exactly what production wires with no key
    app = create_app()
    app.dependency_overrides[get_container] = lambda: container
    client = TestClient(app)
    analysis = client.post(f"/v1/patients/{pid}/analyses").json()
    ex = client.post(f"/v1/patients/{pid}/analyses/{analysis['analysisId']}/explanation").json()
    assert analysis["aiExplanation"] is None and ex["mode"] == "mock" and ex["fallbackCode"] is None


# ---- Claude latency cannot delay or change deterministic results ----------------------------------------------
def test_analysis_returns_promptly_even_if_the_model_would_hang(settings):
    hung = Blocking()
    client = client_for(settings, hung)
    started = time.perf_counter()
    r = client.post("/v1/patients/P001/analyses")
    assert r.status_code == 200 and time.perf_counter() - started < 2
    assert hung.calls == 0 and not hung.entered.is_set()
    hung.release.set()


def test_slow_explanation_does_not_delay_or_alter_deterministic_endpoints(settings):
    slow = Blocking()
    client = client_for(settings, slow)
    analysis = client.post("/v1/patients/P001/analyses").json()
    before = saved(settings, "P001", analysis["analysisId"])

    result: dict = {}
    thread = threading.Thread(target=lambda: result.update(
        r=client.post(f"/v1/patients/P001/analyses/{analysis['analysisId']}/explanation")))
    thread.start()
    assert slow.entered.wait(timeout=10)  # the explanation request is now stuck inside the "model"

    started = time.perf_counter()
    latest = client.get("/v1/patients/P001/analyses/latest")
    other = client.post("/v1/patients/P002/analyses")
    snapshot = client.get("/v1/patients/P001/snapshot")
    elapsed = time.perf_counter() - started
    assert (latest.status_code, other.status_code, snapshot.status_code) == (200, 200, 200)
    assert elapsed < 2, f"deterministic endpoints waited on the slow explanation ({elapsed:.1f}s)"
    assert deterministic(latest.json()) == deterministic(analysis) and latest.json()["aiExplanation"] is None
    assert other.json()["overallSeverity"] == "HIGH"

    slow.release.set()
    thread.join(timeout=10)
    assert result["r"].status_code == 200
    after = saved(settings, "P001", analysis["analysisId"])
    assert deterministic(after) == deterministic(before)  # only aiExplanation was added
    assert after["aiExplanation"] is not None


# ---- failures affect only the explanation ------------------------------------------------------------------
def test_explainer_crash_affects_only_the_explanation(settings):
    class Crashes(ExplanationService):
        def explain(self, *a, **k):
            raise RuntimeError("model exploded")

    client = client_for(settings, Crashes())
    analysis = client.post("/v1/patients/P008/analyses").json()
    before = saved(settings, "P008", analysis["analysisId"])
    r = client.post(f"/v1/patients/P008/analyses/{analysis['analysisId']}/explanation")
    assert r.status_code == 503 and "exploded" not in r.text
    assert saved(settings, "P008", analysis["analysisId"]) == before  # record untouched, byte for byte
    latest = client.get("/v1/patients/P008/analyses/latest").json()
    assert deterministic(latest) == deterministic(analysis) and latest["aiExplanation"] is None


def test_a_hostile_explainer_cannot_alter_the_saved_findings(settings):
    class Mutates(ExplanationService):
        def explain(self, snapshot, analysis, note_context=None):
            analysis.findings.clear()
            analysis.data_gaps.clear()
            analysis.overall_severity = "NONE"
            analysis.summary.high = 99
            return MockExplanationService().explain(snapshot, analysis, note_context)

    client = client_for(settings, Mutates())
    analysis = client.post("/v1/patients/P008/analyses").json()
    r = client.post(f"/v1/patients/P008/analyses/{analysis['analysisId']}/explanation")
    assert r.status_code == 200
    after = saved(settings, "P008", analysis["analysisId"])
    assert deterministic(after) == deterministic(analysis)
    assert after["overallSeverity"] == "HIGH" and len(after["findings"]) == 2 and after["summary"]["high"] == 1


def test_retry_after_a_failure_works_and_a_success_is_not_regenerated(settings):
    class Flaky(ExplanationService):
        def __init__(self):
            self.calls = 0

        def explain(self, snapshot, analysis, note_context=None):
            self.calls += 1
            if self.calls == 1:
                raise ConnectionError("network down")
            return MockExplanationService().explain(snapshot, analysis, note_context)

    flaky = Flaky()
    client = client_for(settings, SafeExplanationService(flaky, MockExplanationService()))
    aid = client.post("/v1/patients/P001/analyses").json()["analysisId"]
    url = f"/v1/patients/P001/analyses/{aid}/explanation"
    first, second, third = client.post(url).json(), client.post(url).json(), client.post(url).json()
    assert first["fallbackCode"] == "EXPLANATION_TEMPORARILY_UNAVAILABLE"  # transient failure -> retry offered
    assert second["fallbackCode"] is None                                  # retry succeeded
    assert third == second and flaky.calls == 2                            # a stored success is not regenerated


# ---- addressing / safety of the new endpoint -------------------------------------------------------------------
@pytest.mark.parametrize("path", [
    "/v1/patients/P001/analyses/AN-P001-999/explanation",           # never created
    "/v1/patients/P001/analyses/AN-P002-001/explanation",           # another patient's id
    "/v1/patients/P001/analyses/not-an-id/explanation",
    "/v1/patients/P001/analyses/..%2f..%2fetc%2fpasswd/explanation",  # traversal attempt
    "/v1/patients/P999/analyses/AN-P999-001/explanation",           # unknown patient
])
def test_explanation_endpoint_404s_for_unknown_or_foreign_analyses(settings, path):
    client = client_for(settings, MockExplanationService())
    client.post("/v1/patients/P001/analyses")
    client.post("/v1/patients/P002/analyses")
    assert client.post(path).status_code == 404
