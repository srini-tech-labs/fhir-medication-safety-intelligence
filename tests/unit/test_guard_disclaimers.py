"""Disclaimer normalisation in the grounding guard's treatment/diagnosis scan.

A *denial* of advice ("No treatment, dosing, or discontinuation guidance is provided here.") must not be rejected, but
genuine advice or a diagnosis claim must still be, even when it is sitting right next to a disclaimer. The normalisation
applies to a private copy of the text used by that one scan; the model output shown to users is never modified.
"""
from __future__ import annotations

import copy

import pytest

from app.services.explanation.base import GroundingViolation
from app.services.explanation.guard import build_model_input, strip_denials, validate_grounding
from tests.unit.test_explanation import (
    TERMS, FakeClient, analysis_and_snapshot, claude, payload_from_mock, test_guard_rejects_ungrounded_output,
)

LEGIT_1 = "No treatment, dosing, or discontinuation guidance is provided here."
LEGIT_2 = "This is a rule-based flag, not a diagnosis or treatment recommendation."


def with_sentence(container, pid: str, sentence: str) -> tuple[dict, dict]:
    """Grounded mock payload for `pid` with `sentence` appended to the first explanation (or the summary if none)."""
    payload, model_input = payload_from_mock(container, pid)
    if payload["findingExplanations"]:
        payload["findingExplanations"][0]["explanation"] += " " + sentence
    else:
        payload["summary"] += " " + sentence
    return payload, model_input


def verdict(container, pid: str, sentence: str) -> str | None:
    payload, model_input = with_sentence(container, pid, sentence)
    try:
        validate_grounding(payload, model_input, TERMS)
        return None
    except GroundingViolation as exc:
        return str(exc)


# ---- legitimate disclaimers are accepted ---------------------------------------------------------------------
@pytest.mark.parametrize("sentence", [
    LEGIT_1,
    LEGIT_2,
    "The engine does not provide treatment or dosing guidance.",
    "This explanation offers no diagnosis.",
    "It is not medical advice or a diagnosis.",
    "This is not a treatment recommendation.",
    "No diagnostic or treatment instructions are included.",
    "Severity is reported exactly as configured and no management, dosing, or discontinuation guidance is provided here.",  # live P005
    "No therapy or prescribing advice is given.",
])
@pytest.mark.parametrize("pid", ["P001", "P008", "P010"])
def test_clear_disclaimers_are_accepted(container, pid, sentence):
    assert verdict(container, pid, sentence) is None


def test_the_two_reported_live_false_positives_are_accepted_verbatim(container):
    """The exact sentences rejected in the live runs (Opus: LEGIT_1, Sonnet: LEGIT_2)."""
    assert verdict(container, "P006", LEGIT_1) is None
    assert verdict(container, "P001", LEGIT_2) is None


# ---- genuine advice / diagnosis claims still fail, in a context where the named drugs ARE supplied -------------
# (so the rejection can only come from the treatment/diagnosis scan, not from an unsupplied-drug-name check)
@pytest.mark.parametrize("pid,sentence,category", [
    ("P001", "No treatment recommendation: discontinue lisinopril.", "treatment/dosing language"),
    ("P002", "This is not a diagnosis, but start warfarin.", "treatment/dosing language"),
    ("P001", "No dosing advice, except take two tablets.", "treatment/dosing language"),
    ("P008", "Without treatment guidance, I recommend stopping ibuprofen.", "treatment/dosing language"),
    ("P001", "This is not a diagnosis; the patient has hyperkalemia.", "diagnosis language"),
])
def test_required_adversarial_sentences_still_fail(container, pid, sentence, category):
    message = verdict(container, pid, sentence)
    assert message is not None and category in message


@pytest.mark.parametrize("pid,sentence,category", [
    ("P001", "This is not a treatment recommendation and lisinopril should be stopped.", "treatment"),
    ("P001", "Not a diagnosis or treatment recommendation to stop lisinopril.", "treatment"),
    ("P001", "There is no guidance, so discontinue lisinopril.", "treatment"),
    ("P001", "No advice is given, however you should not take lisinopril.", "treatment"),
    ("P008", "This is not a diagnosis. We advise reducing ibuprofen.", "treatment"),
    ("P001", "No treatment guidance is provided; I suggest lisinopril be held.", "treatment"),
    ("P001", "This is not a diagnosis; the patient has chronic kidney disease.", "diagnosis"),
    ("P001", "No advice is given, but she is diagnosed with hyperkalemia.", "diagnosis"),
    ("P001", "This does not constitute a diagnosis, although the patient suffers from hyperkalemia.", "diagnosis"),
    ("P001", "It is not a diagnosis, yet the patient has developed acute kidney injury.", "diagnosis"),
    ("P001", "No management guidance: discontinue lisinopril.", "treatment"),
    ("P001", "There is no therapy advice, so stop the drug.", "treatment"),
    ("P008", "No medication guidance is provided; take ibuprofen with food.", "treatment"),
])
def test_further_bypass_attempts_using_a_disclaimer_as_cover_still_fail(container, pid, sentence, category):
    message = verdict(container, pid, sentence)
    assert message is not None and category in message


# ---- normalisation is narrow, pure, and never touches the model output --------------------------------------------
def test_strip_denials_removes_exactly_the_denial_span():
    assert strip_denials(LEGIT_1).strip(" .") == "is provided here"
    assert strip_denials(LEGIT_2).strip(" .,") == "This is a rule-based flag"
    assert strip_denials("No treatment recommendation: discontinue lisinopril.") == " : discontinue lisinopril."
    assert strip_denials("This is not a diagnosis, but start warfarin.") == "This is  , but start warfarin."


@pytest.mark.parametrize("text", [
    "Lisinopril should not be discontinued abruptly.",       # advice, no denied guidance noun
    "The patient should stop taking ibuprofen.",
    "Stop the drug.",
    "The rule fired because potassium is above the threshold.",
    "No configured rule fired for this patient.",
    "It does not add findings or change severity.",
])
def test_strip_denials_leaves_everything_else_alone(text):
    assert strip_denials(text) == text


def test_normalisation_never_alters_the_model_output_or_the_analysis(container):
    payload, model_input = with_sentence(container, "P008", LEGIT_1 + " " + LEGIT_2)
    payload_before, input_before = copy.deepcopy(payload), copy.deepcopy(model_input)
    validate_grounding(payload, model_input, TERMS)
    assert payload == payload_before and model_input == input_before  # nothing mutated
    assert LEGIT_1 in payload["findingExplanations"][0]["explanation"]   # disclaimer still present, verbatim


def test_live_path_returns_the_model_text_verbatim_including_the_disclaimer(container):
    payload, _ = with_sentence(container, "P001", LEGIT_1)
    snap, analysis = analysis_and_snapshot(container, "P001")
    before = analysis.model_dump()
    ex = claude(FakeClient(payload)).explain(snap, analysis)
    assert ex.mode == "llm" and ex.fallback_code is None
    assert ex.finding_explanations[0].explanation == payload["findingExplanations"][0]["explanation"]
    assert LEGIT_1 in ex.text
    assert analysis.model_dump() == before  # deterministic analysis untouched


# ---- realistic legitimate sentences must not become false positives ------------------------------------------
@pytest.mark.parametrize("pid,sentence", [
    ("P008", "The lisinopril label recommends periodic serum potassium monitoring."),
    ("P008", "Renal function should be monitored while both medications are active."),
    ("P008", "The patient is taking lisinopril 20 mg once daily and ibuprofen as needed."),
    ("P002", "Aspirin can increase bleeding risk when used with warfarin."),
    ("P008", "This explanation restates the deterministic findings and does not add findings or change severity."),
    ("P009", "The absence of findings is not an affirmative determination that the regimen is clinically safe."),
    ("P008", "The medication reconciliation note is context only and did not generate any finding."),
    ("P010", "The engine reported a data gap rather than estimating a potassium value."),
    ("P001", "Evidence EVID-002 cites DailyMed/FDA lisinopril labeling."),
    ("P001", "The patient has an active order for lisinopril and no recent potassium result."),
])
def test_ordinary_grounded_sentences_are_not_flagged(container, pid, sentence):
    assert verdict(container, pid, sentence) is None


# ---- the existing 12 rejection cases: identical behaviour --------------------------------------------------------
# Messages recorded from the guard BEFORE this change; every one must still be produced exactly.
OLD_MESSAGES = {
    0: "finding rule IDs ['DL-009'] != supplied ['DL-001']; severity HIGH not supplied for this text",
    1: "finding rule IDs [] != supplied ['DL-001']",
    2: "severity MODERATE not supplied for this text",
    3: "severity phrase 'low' not supplied for this text",
    4: "number 6.9 not in supplied data",
    5: "'Warfarin' is not part of this patient's supplied data",
    6: "treatment/dosing language",
    7: "treatment/dosing language",
    8: "diagnosis language",
    9: "claims the regimen is safe",
    10: "groundedInFindingsOnly is not true",
    11: "data gap explained but none was supplied",
}


def test_the_existing_twelve_rejection_cases_are_unchanged(container):
    cases = [m for m in test_guard_rejects_ungrounded_output.pytestmark if m.name == "parametrize"][0].args[1]
    assert len(cases) == 12 == len(OLD_MESSAGES)
    for i, (mutate, _reason) in enumerate(cases):
        payload, model_input = payload_from_mock(container, "P001")
        mutate(payload)
        with pytest.raises(GroundingViolation) as exc:
            validate_grounding(payload, model_input, TERMS)
        assert str(exc.value) == OLD_MESSAGES[i], f"rejection case {i + 1} changed"
