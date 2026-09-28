"""Rule-specific evidence: each finding's model input carries only evidence text about that finding's own medications.

Regression for the live P003 false positive: EVID-001 (shared by warfarin+aspirin and warfarin+ibuprofen) named aspirin, the
model quoted it while explaining warfarin+ibuprofen, and the (correct) unsupplied-drug guard rejected the explanation. The
guard is NOT weakened; the model simply no longer receives text about drugs that are not part of the finding.
"""
from __future__ import annotations

import itertools
import json
import re

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_container
from app.main import create_app
from app.services.explanation.base import GroundingViolation
from app.services.explanation.evidence_scope import scope_summary, scoped_evidence
from app.services.explanation.guard import build_model_input, validate_grounding
from tests.conftest import PACKAGE_DIR, load_json
from tests.unit.test_explanation import TERMS, FakeClient, analysis_and_snapshot, claude, payload_from_mock

CATALOG = load_json(PACKAGE_DIR / "rules" / "rule_evidence.json")["evidence"]
UNIVERSE = set(TERMS.drugs.values())
EVID_001 = "The warfarin label lists aspirin among antiplatelet agents and ibuprofen among NSAIDs that increase bleeding risk when used with warfarin."


def mentions(text: str | None, name: str) -> bool:
    return bool(text) and re.search(rf"\b{re.escape(name)}\b", text, re.I) is not None


# ---- the two real rules that share EVID-001 ---------------------------------------------------------------------
def test_ddi_002_warfarin_ibuprofen_gets_no_aspirin_text():
    text, scope = scope_summary(EVID_001, {"Warfarin", "Ibuprofen"}, UNIVERSE)
    assert scope == "pruned"
    assert text == "The warfarin label lists ibuprofen among NSAIDs that increase bleeding risk when used with warfarin."
    assert not mentions(text, "aspirin")


def test_ddi_001_warfarin_aspirin_gets_no_ibuprofen_text():
    text, scope = scope_summary(EVID_001, {"Warfarin", "Aspirin"}, UNIVERSE)
    assert scope == "pruned" and text == "The warfarin label lists aspirin among antiplatelet agents."
    assert not mentions(text, "ibuprofen")


def test_evidence_that_only_names_the_findings_own_drugs_is_sent_unchanged():
    for e in CATALOG:
        if e["evidenceId"] != "EVID-001":
            own = {d for d in UNIVERSE if mentions(e["summary"], d)}
            assert scope_summary(e["summary"], own, UNIVERSE) == (e["summary"], "full")


# ---- property: never a drug outside the allowed set, never new words, over the WHOLE catalog x EVERY drug subset ------
ALL_SUBSETS = [set(c) for r in range(len(UNIVERSE) + 1) for c in itertools.combinations(sorted(UNIVERSE), r)]


@pytest.mark.parametrize("entry", CATALOG, ids=[e["evidenceId"] for e in CATALOG])
def test_scoped_text_never_names_a_drug_outside_the_allowed_set(entry):
    for allowed in ALL_SUBSETS:
        text, scope = scope_summary(entry["summary"], allowed, UNIVERSE)
        assert scope in ("full", "pruned", "withheld")
        if text is not None:
            for drug in UNIVERSE - allowed:
                assert not mentions(text, drug), f"{entry['evidenceId']} allowed={sorted(allowed)}: leaked {drug}"
            # pruning only removes words: what remains is the original text, in order, never reworded
            it = iter(re.findall(r"[\w'-]+", entry["summary"].lower()))
            assert all(word in it for word in re.findall(r"[\w'-]+", text.lower()))
        else:
            assert scope == "withheld"


# ---- adversarial evidence shapes: prune when clean, otherwise withhold, never leak ---------------------------------
@pytest.mark.parametrize("summary,allowed,expect_scope", [
    ("The label lists aspirin among antiplatelet agents, ibuprofen among NSAIDs and warfarin among anticoagulants.", {"Warfarin"}, None),
    ("The label lists insulin glargine among long-acting insulins and metformin among biguanides.", {"Metformin"}, "pruned"),
    ("Aspirin should not be combined with warfarin.", {"Warfarin"}, "withheld"),      # cannot prune: withhold
    ("Do not use with ASPIRIN or ibuprofen; warfarin levels rise.", {"Warfarin"}, "withheld"),
    ("The warfarin label lists aspirin among antiplatelets.", {"Warfarin"}, "withheld"),  # remainder too short to be useful
])
def test_adversarial_evidence_text_is_pruned_or_withheld_never_leaked(summary, allowed, expect_scope):
    text, scope = scope_summary(summary, allowed, UNIVERSE)
    for drug in UNIVERSE - allowed:
        assert not mentions(text, drug)
    if expect_scope:
        assert scope == expect_scope
    assert (text is None) == (scope == "withheld")


def test_a_withheld_entry_still_sends_the_citation():
    ev = [type("E", (), dict(evidence_id="EVID-X", source="DailyMed", section="S", summary="Aspirin should not be combined with warfarin."))()]
    [entry] = scoped_evidence(ev, ["Warfarin"], TERMS)
    assert entry == {"evidenceId": "EVID-X", "source": "DailyMed", "section": "S", "summary": None, "summaryScope": "withheld"}


# ---- integration: what the model is actually sent, for the real golden patients ------------------------------------
def sent_for(container, pid):
    a = container.analyses.analyze(pid)
    return a, build_model_input(container.snapshots.snapshot(pid), a, None, TERMS)


def test_p003_model_input_contains_no_aspirin_but_p002_still_gets_aspirin_text(container):
    _, mi3 = sent_for(container, "P003")
    assert not mentions(json.dumps(mi3["deterministicFindings"]), "aspirin")
    assert mentions(json.dumps(mi3["deterministicFindings"]), "ibuprofen")
    _, mi2 = sent_for(container, "P002")
    assert mentions(json.dumps(mi2["deterministicFindings"]), "aspirin")
    assert not mentions(json.dumps(mi2["deterministicFindings"]), "ibuprofen")


@pytest.mark.parametrize("pid", [f"P{n:03d}" for n in range(1, 11)])
def test_every_golden_finding_only_receives_evidence_about_its_own_medications(container, pid):
    analysis, mi = sent_for(container, pid)
    for finding, sent in zip(analysis.findings, mi["deterministicFindings"]):
        own = {m.name for m in finding.evidence.medications}
        for drug in UNIVERSE - own:
            assert not mentions(json.dumps(sent["evidence"]), drug), f"{pid} {finding.rule_id} was sent text about {drug}"


def test_the_full_catalog_is_preserved_for_provenance_and_display(container, settings):
    analysis, _ = sent_for(container, "P003")
    (evidence,) = analysis.findings[0].provenance.evidence
    assert evidence.summary == EVID_001                                   # full text incl. aspirin, untouched
    assert mentions(evidence.summary, "aspirin") and mentions(evidence.summary, "ibuprofen")
    app = create_app(); app.dependency_overrides[get_container] = lambda: container
    body = TestClient(app).post("/v1/patients/P003/analyses").json()      # what the UI receives
    assert body["findings"][0]["provenance"]["evidence"][0]["summary"] == EVID_001
    frozen = next(e for e in CATALOG if e["evidenceId"] == "EVID-001")
    assert frozen["summary"] == EVID_001                                  # the frozen catalog itself


def test_the_request_actually_sent_to_the_model_has_no_aspirin_for_p003(container):
    snap, analysis = analysis_and_snapshot(container, "P003")
    fake = FakeClient(payload_from_mock(container, "P003")[0])
    claude(fake).explain(snap, analysis)
    sent = fake.calls[0]["messages"][0]["content"]
    assert not mentions(sent, "aspirin") and mentions(sent, "ibuprofen")


# ---- the unsupplied-drug guard is NOT weakened --------------------------------------------------------------------
@pytest.mark.parametrize("sentence", [
    "Aspirin can also increase bleeding risk.",
    "The warfarin label lists aspirin among antiplatelet agents.",   # exactly what the model used to quote
])
def test_a_model_that_mentions_aspirin_for_p003_is_still_rejected(container, sentence):
    payload, model_input = payload_from_mock(container, "P003")
    payload["findingExplanations"][0]["explanation"] += " " + sentence
    with pytest.raises(GroundingViolation, match="'Aspirin' is not part of this patient's supplied data"):
        validate_grounding(payload, model_input, TERMS)
