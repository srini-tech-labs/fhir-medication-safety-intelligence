"""AI explanation layer: ordering, grounding guard, Claude adapter (fake client), fallbacks."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.models.contract import Analysis
from app.services.explanation.base import ExplanationService, GroundingViolation
from app.services.explanation.claude import ClaudeExplanationService, ExplanationUnavailable
from app.services.explanation.factory import SafeExplanationService, create_explanation_service
from app.services.explanation.guard import build_model_input, validate_grounding
from app.services.explanation.mock import MockExplanationService
from app.terminology import Terminology
from tests.conftest import PACKAGE_DIR

TERMS = Terminology.load(PACKAGE_DIR)
ALL = [f"P{n:03d}" for n in range(1, 11)]


def analysis_and_snapshot(container, pid):
    a = container.analyses.run(pid)
    return container.snapshots.snapshot(pid), a.model_copy(update={"ai_explanation": None})


def payload_from_mock(container, pid) -> tuple[dict, dict]:
    snap, a = analysis_and_snapshot(container, pid)
    ex = MockExplanationService().explain(snap, a, None)
    payload = {
        "summary": ex.summary,
        "findingExplanations": [{"ruleId": e.rule_id, "explanation": e.explanation} for e in ex.finding_explanations],
        "dataGapExplanation": ex.data_gap_explanation,
        "groundedInFindingsOnly": True,
    }
    return payload, build_model_input(snap, a, None, TERMS)


class FakeClient:
    def __init__(self, payload=None, stop_reason="end_turn", text=None):
        self.calls = []
        self._resp = SimpleNamespace(
            stop_reason=stop_reason, model="claude-opus-5",
            content=[SimpleNamespace(type="text", text=text if text is not None else json.dumps(payload))],
        )
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kw):
        self.calls.append(kw)
        return self._resp


def claude(client):
    return ClaudeExplanationService(PACKAGE_DIR, TERMS, "claude-opus-5", client=client)


# ---- ordering / non-fatal ---------------------------------------------------------------------
def test_ai_runs_only_after_deterministic_results_are_complete_and_persisted(container, settings):
    seen = {}

    class Spy(ExplanationService):
        def explain(self, snapshot, analysis, note_context=None):
            seen["findings"] = [f.rule_id for f in analysis.findings]
            seen["overall"] = analysis.overall_severity
            # analyze() no longer writes FHIR outputs at all (that's the separate, explicit persist_to_fhir()
            # path) -- confirms explain() sees a complete deterministic result without any clinical-store write.
            seen["issues_written"] = (settings.output_dir / "detected_issues").exists()
            seen["ra_written"] = (settings.output_dir / "risk_assessments").exists()
            seen["ai_field"] = analysis.ai_explanation
            return MockExplanationService().explain(snapshot, analysis, note_context)

    from app.container import build_container
    build_container(settings, explainer=Spy()).analyses.run("P008")
    assert seen["findings"] == ["DL-001", "DDI-003"] and seen["overall"] == "HIGH"
    assert seen["issues_written"] is False and seen["ra_written"] is False
    assert seen["ai_field"] is None


def test_explainer_failure_never_breaks_the_deterministic_result(settings):
    class Boom(ExplanationService):
        def explain(self, *a, **k):
            raise RuntimeError("model down")

    from app.container import build_container
    r = build_container(settings, explainer=Boom()).analyses.run("P001")
    assert r.overall_severity == "HIGH" and [f.rule_id for f in r.findings] == ["DL-001"]
    assert r.ai_explanation is None


# ---- mock ---------------------------------------------------------------------------------------
@pytest.mark.parametrize("pid", ALL)
def test_mock_explanation_is_grounded_and_passes_the_guard(container, pid):
    payload, model_input = payload_from_mock(container, pid)
    validate_grounding(payload, model_input, TERMS)  # must not raise


def test_mock_no_findings_says_no_rule_fired_and_never_claims_safety(container):
    ex = container.analyses.run("P009").ai_explanation
    assert ex.mode == "mock" and ex.grounded_in_findings_only is True
    assert "No configured rule fired" in ex.summary
    assert "clinically safe" in ex.summary and "not a statement" in ex.summary
    assert ex.finding_explanations == [] and ex.data_gap_explanation is None


def test_mock_p010_explains_gap_without_a_potassium_value(container):
    ex = container.analyses.run("P010").ai_explanation
    assert "DG-001" in ex.data_gap_explanation and "No value has been estimated" in ex.data_gap_explanation
    assert "mmol/L" not in ex.text  # no potassium reading is ever stated
    assert "context only" in ex.summary  # the pending-panel note is context, not a result


# ---- guard --------------------------------------------------------------------------------------
@pytest.mark.parametrize("mutate,reason", [
    (lambda p: p["findingExplanations"][0].update(ruleId="DL-009"), "rule IDs"),
    (lambda p: p["findingExplanations"].clear(), "rule IDs"),
    (lambda p: p["findingExplanations"][0].update(explanation="Rule DL-001 is actually MODERATE here."), "severity"),
    (lambda p: p["findingExplanations"][0].update(explanation="This is a low risk situation."), "severity"),
    (lambda p: p["findingExplanations"][0].update(explanation="Potassium was 6.9 mmol/L."), "number"),
    (lambda p: p["findingExplanations"][0].update(explanation="Warfarin adds to the concern."), "not part of"),
    (lambda p: p["findingExplanations"][0].update(explanation="The clinician should discontinue lisinopril."), "treatment"),
    (lambda p: p["findingExplanations"][0].update(explanation="Consider reducing the dose of lisinopril."), "treatment"),
    (lambda p: p["findingExplanations"][0].update(explanation="This confirms a diagnosis of hyperkalemia."), "diagnosis"),
    (lambda p: p.update(summary="The regimen is clinically safe otherwise."), "safe"),
    (lambda p: p.update(groundedInFindingsOnly=False), "grounded"),
    (lambda p: p.update(dataGapExplanation="Potassium is probably normal."), "none was supplied"),
])
def test_guard_rejects_ungrounded_output(container, mutate, reason):
    payload, model_input = payload_from_mock(container, "P001")
    mutate(payload)
    with pytest.raises(GroundingViolation, match=reason):
        validate_grounding(payload, model_input, TERMS)


def test_guard_requires_data_gap_to_be_explained(container):
    payload, model_input = payload_from_mock(container, "P010")
    payload["dataGapExplanation"] = None
    with pytest.raises(GroundingViolation, match="not explained"):
        validate_grounding(payload, model_input, TERMS)


def test_guard_allows_negated_safety_language(container):
    payload, model_input = payload_from_mock(container, "P001")
    payload["summary"] += " This does not mean the regimen is safe."
    validate_grounding(payload, model_input, TERMS)


# ---- Claude adapter (fake client; no network, no key) ------------------------------------------
def test_claude_adapter_uses_frozen_prompt_schema_output_and_finished_findings(container):
    payload, _ = payload_from_mock(container, "P008")
    fake = FakeClient(payload)
    snap, a = analysis_and_snapshot(container, "P008")
    ex = claude(fake).explain(snap, a, "Discharge Summary: Potassium was elevated on follow-up testing.")

    assert ex.mode == "llm" and ex.model == "claude-opus-5" and ex.grounded_in_findings_only
    (call,) = fake.calls
    assert call["system"] == (PACKAGE_DIR / "prompts" / "ai_explanation_system.txt").read_text()
    assert call["output_config"]["format"]["type"] == "json_schema"
    sent = json.loads(call["messages"][0]["content"])
    assert [f["ruleId"] for f in sent["deterministicFindings"]] == ["DL-001", "DDI-003"]
    assert sent["overallSeverity"] == "HIGH" and "Potassium was elevated" in sent["unstructuredContext"]
    assert [e.rule_id for e in ex.finding_explanations] == ["DL-001", "DDI-003"]


def test_claude_refusal_or_truncation_or_bad_json_is_unavailable(container):
    snap, a = analysis_and_snapshot(container, "P001")
    for fake in (FakeClient({}, stop_reason="refusal"), FakeClient({}, stop_reason="max_tokens"),
                 FakeClient(text="not json")):
        with pytest.raises(ExplanationUnavailable):
            claude(fake).explain(snap, a)


def test_safe_service_falls_back_to_labelled_mock_on_violation_and_error(container):
    snap, a = analysis_and_snapshot(container, "P001")
    bad = payload_from_mock(container, "P001")[0]
    bad["findingExplanations"][0]["explanation"] = "Also consider warfarin interactions."

    class Raises(ExplanationService):
        def explain(self, *a, **k):
            raise ConnectionError("network down")

    for primary in (claude(FakeClient(bad)), claude(FakeClient({}, stop_reason="refusal")), Raises()):
        ex = SafeExplanationService(primary, MockExplanationService()).explain(snap, a)
        assert ex.mode == "mock" and ex.fallback_reason
        assert [e.rule_id for e in ex.finding_explanations] == ["DL-001"]


def test_safe_service_passes_through_valid_llm_output(container):
    snap, a = analysis_and_snapshot(container, "P001")
    good = payload_from_mock(container, "P001")[0]
    ex = SafeExplanationService(claude(FakeClient(good)), MockExplanationService()).explain(snap, a)
    assert ex.mode == "llm" and ex.fallback_reason is None


# ---- factory ------------------------------------------------------------------------------------
def test_factory_uses_mock_without_credentials_and_claude_with_them(settings, monkeypatch):
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    auto = type(settings)(**{**settings.__dict__, "explanation_mode": "auto"})
    assert isinstance(create_explanation_service(auto), MockExplanationService)

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")
    assert isinstance(create_explanation_service(auto), SafeExplanationService)
    forced_mock = type(settings)(**{**settings.__dict__, "explanation_mode": "mock"})
    assert isinstance(create_explanation_service(forced_mock), MockExplanationService)


# ---- notes cannot ground numbers or facts ---------------------------------------------------------
def test_guard_rejects_a_lab_value_that_only_appears_in_a_note(container):
    snap, a = analysis_and_snapshot(container, "P010")
    note = "Addendum: the pending panel returned potassium 4.1 mmol/L."
    model_input = build_model_input(snap, a, note, TERMS)
    payload = {
        "summary": "One configured check could not be completed.",
        "findingExplanations": [],
        "dataGapExplanation": "Rule DG-001 needs a Potassium result; the note reports potassium 4.1 mmol/L.",
        "groundedInFindingsOnly": True,
    }
    with pytest.raises(GroundingViolation, match="number 4.1"):
        validate_grounding(payload, model_input, TERMS)


def test_guard_still_accepts_note_context_that_adds_no_new_facts(container):
    payload, _ = payload_from_mock(container, "P008")
    snap, a = analysis_and_snapshot(container, "P008")
    note = "Recent discharge summary lists lisinopril as chronic and ibuprofen as as-needed."
    validate_grounding(payload, build_model_input(snap, a, note, TERMS), TERMS)
