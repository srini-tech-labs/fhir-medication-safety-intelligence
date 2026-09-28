"""The live harness's second-opinion scan must ignore disclaimers but still flag real advice (independent of the guard)."""
from __future__ import annotations

import importlib.util
from types import SimpleNamespace

import pytest

from app.config import REPO_ROOT

spec = importlib.util.spec_from_file_location("live_claude_check", REPO_ROOT / "scripts" / "live_claude_check.py")
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)

MODEL_INPUT = {"deterministicFindings": [{"ruleId": "DL-001"}], "dataGaps": [],
               "structuredData": {"medications": [{"name": "Lisinopril"}], "labs": [{"name": "Potassium", "value": 5.8}]}}


def flags(text: str) -> list[str]:
    ai = SimpleNamespace(summary=text, finding_explanations=[], data_gap_explanation=None)
    return harness.independent_flags(ai, MODEL_INPUT)


@pytest.mark.parametrize("text", [
    "No treatment, dosing, or discontinuation guidance is provided here.",
    "This is a rule-based flag, not a diagnosis or treatment recommendation.",
    "Elevated potassium is a concern in a patient receiving lisinopril; no dosing or discontinuation guidance is provided here.",
    "This is a deterministic rule output, not a treatment, dosing, or discontinuation recommendation.",
])
def test_disclaimers_are_not_flagged(text):
    assert flags(text) == []


@pytest.mark.parametrize("text", [
    "No treatment recommendation: discontinue lisinopril.",
    "There is no guidance, so discontinue lisinopril.",
    "This is not a diagnosis; the patient has a diagnosis of hyperkalemia.",
    "Lisinopril should be discontinued.",
    "Stop taking lisinopril.",
])
def test_real_advice_is_still_flagged(text):
    assert "treatment/diagnosis wording" in flags(text)
