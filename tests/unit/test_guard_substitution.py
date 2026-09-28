"""Treatment-substitution detection is context sensitive.

Regression for the live rejection of "…unstructured narrative cannot substitute for a structured lab result…" (the broad
`substitut\\w+` pattern). Genuine substitution advice ("substitute X for Y", "replace X with Y", "switch from X to Y") must still
be rejected, including when it hides next to a legitimate sentence.
"""
from __future__ import annotations

import pytest

from app.services.explanation.base import GroundingViolation
from app.services.explanation.guard import _SUBSTITUTION, _TREATMENT, validate_grounding
from tests.unit.test_explanation import TERMS, payload_from_mock


def verdict(container, pid: str, sentence: str) -> str | None:
    payload, model_input = payload_from_mock(container, pid)
    key = "explanation" if payload["findingExplanations"] else None
    if key:
        payload["findingExplanations"][0]["explanation"] += " " + sentence
    else:
        payload["summary"] += " " + sentence
    try:
        validate_grounding(payload, model_input, TERMS)
        return None
    except GroundingViolation as exc:
        return str(exc)


# ---- ordinary explanatory language is accepted -------------------------------------------------------------------
@pytest.mark.parametrize("pid,sentence", [
    ("P010", "Note text in the record asserts a potassium value and a safety conclusion, but unstructured narrative cannot substitute "
             "for a structured lab result or resolve this data gap; no value is being adopted from it."),   # the exact live sentence
    ("P010", "Unstructured narrative cannot substitute for a structured lab result."),
    ("P010", "A clinical note cannot be substituted for a structured potassium result."),
    ("P008", "The note is not a substitute for the structured medication list."),
    ("P010", "Estimated values are never used to replace a missing lab result."),
    ("P008", "This explanation does not replace clinical judgment."),
    ("P010", "The engine does not substitute an estimated value for the missing potassium result."),
    ("P010", "No substitution of structured data by free text occurs."),
    ("P001", "A missing value cannot be replaced with an assumption."),
])
def test_ordinary_substitution_language_is_accepted(container, pid, sentence):
    assert verdict(container, pid, sentence) is None


def test_the_broad_pattern_is_gone_from_the_treatment_regex():
    assert not _TREATMENT.search("a narrative cannot substitute for a structured lab result")
    assert not _SUBSTITUTION.search("a narrative cannot substitute for a structured lab result")


# ---- genuine treatment-substitution advice is rejected -----------------------------------------------------------
@pytest.mark.parametrize("pid,sentence", [
    ("P008", "Substitute acetaminophen for ibuprofen."),
    ("P008", "Replace ibuprofen with acetaminophen."),
    ("P008", "Switch from ibuprofen to acetaminophen."),
    ("P008", "Consider substituting a safer drug for ibuprofen."),
    ("P008", "Ibuprofen should be replaced."),
    ("P008", "Lisinopril should be substituted with an ARB."),
    ("P008", "Swap lisinopril for another agent."),
    ("P008", "Substitute a different treatment."),
    ("P002", "The clinician could replace warfarin with aspirin."),
    ("P003", "Ibuprofen can be swapped for acetaminophen."),
    ("P001", "Lisinopril must be replaced by an alternative medication."),
    ("P008", "An alternative drug would avoid this interaction."),
])
def test_genuine_substitution_advice_is_rejected(container, pid, sentence):
    message = verdict(container, pid, sentence)
    assert message is not None and "treatment/dosing language" in message


@pytest.mark.parametrize("pid,sentence", [
    ("P008", "A note cannot substitute for lab data, so replace ibuprofen with acetaminophen."),
    ("P010", "Unstructured narrative cannot substitute for a structured lab result. Switch from lisinopril to another drug."),
    ("P008", "This is not a substitute for judgment; substitute acetaminophen for ibuprofen."),
    ("P008", "No treatment guidance is provided, but ibuprofen should be replaced."),   # also uses a disclaimer as cover
])
def test_advice_hidden_next_to_a_legitimate_sentence_is_still_rejected(container, pid, sentence):
    message = verdict(container, pid, sentence)
    assert message is not None and "treatment/dosing language" in message


def test_unsupplied_drug_check_is_unchanged_for_substitution_targets(container):
    """The name guard still fires independently: a substitution naming an unsupplied drug is rejected on both counts."""
    message = verdict(container, "P001", "Replace lisinopril with warfarin.")
    assert "'Warfarin' is not part of this patient's supplied data" in message and "treatment/dosing language" in message
