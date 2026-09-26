"""Grounding guard: enforces the AI boundary in code, not just in the prompt.

An LLM explanation is accepted only if it stays inside the deterministic input it was given:
same finding set, no new rule IDs / severities / medications / labs / numbers, and no treatment,
dosing, diagnosis or "regimen is safe" language. Any violation raises ``GroundingViolation`` and
the caller falls back to the deterministic mock explanation.

This is a best-effort lexical check over a small closed vocabulary (the six ingredients and four
labs of the frozen dataset); it complements, and does not replace, the system prompt.
"""
from __future__ import annotations

import json
import re

from app.models.contract import Analysis, Snapshot
from app.services.explanation.base import GroundingViolation
from app.services.explanation.evidence_scope import scoped_evidence
from app.terminology import Terminology

SEVERITIES = ("HIGH", "MODERATE", "LOW")

_RULE_ID = re.compile(r"\b(?:DDI|DL|DG)-\d{3}\b")
_UPPER_SEVERITY = re.compile(r"\b(HIGH|MODERATE|LOW)\b")  # case-sensitive on purpose
_PHRASE_SEVERITY = re.compile(r"\b(high|moderate|low)[\s-]+(?:severity|risk|priority)\b", re.I)
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_TREATMENT = re.compile(
    r"\b(discontinu\w*|stop(?:ping)?\s+(?:taking|using|the)|hold(?:ing)?\s+(?:the\s+)?(?:medication|drug|dose)"
    r"|(?:increas|decreas|reduc|lower|rais|adjust|titrat)\w*\s+(?:the\s+)?(?:dose|dosage|dosing)"
    r"|dose\s+(?:adjustment|reduction|change)|switch(?:ing)?\s+to"
    r"|should\s+(?:be\s+)?(?:stopped|held|discontinued|avoided|started|taken))",
    re.I,
)
_DIAGNOSIS = re.compile(r"\bdiagnos\w*", re.I)

# Treatment SUBSTITUTION advice ("substitute X for Y", "replace X with Y", "switch from X to Y"). A bare "substitut*" is ordinary
# English ("a narrative cannot substitute for a structured lab result"), so the verb only counts when it acts on a medication:
# the medication is its object or its target, or the medication is the subject of "should be replaced".
_SUBST_DRUGISH = (r"(?:lisinopril|warfarin|aspirin|ibuprofen|metformin|insulin(?:\s+glargine)?|"
                  r"nsaids?|antiplatelets?|anticoagulants?|analgesics?|ace\s+inhibitors?)")
_NOT_PREP = r"(?!(?:for|with|by|to|of|from|in|on|as|and|or)\b)"   # object words never start with a preposition
_WORDS_0_2 = rf"(?:{_NOT_PREP}\w+\s+){{0,2}}"
_WORDS_1_3 = rf"(?:{_NOT_PREP}\w+\s+){{1,3}}"
_SUBST_VERB = r"(?:substitut\w*|replac\w*|swap\w*)"
_GENERIC_DRUG = r"(?:medications?|drugs?|therap(?:y|ies)|treatments?|agents?)"
_SUBSTITUTION = re.compile(
    rf"\b{_SUBST_VERB}\s+{_WORDS_0_2}{_SUBST_DRUGISH}\b"                                   # substitute ibuprofen ...; replace lisinopril ...
    rf"|\b{_SUBST_VERB}\s+{_WORDS_1_3}(?:for|with|by)\s+{_WORDS_0_2}{_SUBST_DRUGISH}\b"     # substitute X for ibuprofen
    rf"|\b{_SUBST_VERB}\s+(?:(?:a|an|the)\s+)?(?:different|another|alternative|other|safer|new)\s+{_GENERIC_DRUG}\b"
    rf"|\b{_SUBST_DRUGISH}\b[^.;:]{{0,40}}?\b(?:should|must|needs?\s+to|can|could|will)\s+be\s+(?:substituted|replaced|swapped)\b"
    rf"|\b(?:substituted|replaced|swapped)\s+(?:with|for|by)\s+{_WORDS_0_2}{_SUBST_DRUGISH}\b"  # replaced with ibuprofen
    rf"|\bswitch(?:es|ed|ing)?\s+from\s+{_WORDS_0_2}{_SUBST_DRUGISH}\b"                    # switch from lisinopril to ...
    rf"|\b(?:alternative|safer)\s+{_GENERIC_DRUG}\b",
    re.I,
)

# Directive / recommending language the original patterns missed (e.g. "take two tablets", "I recommend stopping X").
# Deliberately does NOT match reported label content such as "the label recommends periodic monitoring".
_DRUG_OR_DOSE = (r"(?:\d+(?:\.\d+)?\s*(?:mg|mcg|units?|ml)|one|two|three|four|half|tablets?|pills?|capsules?|doses?|dosage|"
                 r"medications?|drugs?|lisinopril|warfarin|aspirin|ibuprofen|metformin|insulin(?:\s+glargine)?)")
_ADVICE = re.compile(
    r"\b(?:i|we)\s+(?:would\s+|strongly\s+)?(?:recommend|suggest|advise|urge)\b"
    r"|\b(?:recommend|advise)(?:s|ed|ing)?\s+(?:that\s+)?(?:the\s+)?(?:patient|clinician|prescriber|physician|you|stopping|"
    r"starting|discontinuing|reducing|increasing|decreasing|switching|holding|avoiding|taking|adding|changing|adjusting|initiating)\b"
    r"|\bshould\s+(?:not\s+)?(?:stop|start|take|discontinue|hold|avoid|begin|switch)\b"
    rf"|\b(?:start|begin|initiate|resume|take|administer|prescribe|stop|cease|hold|avoid|switch\s+to)\s+"
    rf"(?:(?:the|this|that|a|an|your)\s+)?{_DRUG_OR_DOSE}\b",
    re.I,
)
# An assertion that the patient HAS a condition (a diagnosis the rule engine never made), e.g. "the patient has hyperkalemia".
_DIAGNOSIS_CLAIM = re.compile(
    r"\b(?:patient|he|she)\s+(?:(?:has|have|had)(?:\s+(?:developed|been\s+diagnosed\s+with))?|is\s+diagnosed\s+with|"
    r"suffers?\s+from|is\s+suffering\s+from|presents?\s+with|developed)\s+(?:(?:an?|the|severe|acute|chronic|mild|moderate)\s+)*"
    r"(?:(?:hyper|hypo)\w+|\w+emia|\w+osis|\w+itis|\w+pathy|(?:\w+\s+){0,2}(?:disease|failure|insufficiency|injury|syndrome))\b",
    re.I,
)

# ---- disclaimer normalisation (scan text only) ------------------------------------------------------------------
# A *denial* of advice is not advice: "No treatment, dosing, or discontinuation guidance is provided", "not a diagnosis or
# treatment recommendation". Only spans of the exact shape  <negation> <denied guidance noun phrase(s)>  are blanked, and only
# in the private copy the treatment/diagnosis scan reads. Everything that follows the denial stays in the scan text, so
# "No treatment recommendation: discontinue lisinopril." still fails on "discontinue lisinopril".
_MOD = (r"(?:treatment|dosing|dose|dosage|discontinuation|therapeutic|therapy|management|prescribing|medical|clinical|diagnostic|"
        r"patient-specific|medication[- ]change|medication)")
_NOUN = r"(?:diagnos[ie]s|guidance|advice|recommendations?|instructions?)"
_ITEM = rf"(?:(?:a|an|any)\s+)?(?:{_MOD}(?:\s*,\s*(?:or\s+|and\s+)?|\s+(?:or|and)\s+|\s+))*{_NOUN}"
_ITEMS = rf"{_ITEM}(?:\s*,\s*(?:or\s+|and\s+)?{_ITEM}|\s+(?:or|and)\s+{_ITEM})*"
_DENIAL_LEAD = r"(?:\bno\b|\bwithout\b|\bnor\b|\bnot\b|\b(?:does|do|did)\s+not\s+(?:provide|offer|give|constitute|make|include|contain)\b)"
_DENIAL = re.compile(rf"{_DENIAL_LEAD}\s+{_ITEMS}", re.I)


def strip_denials(text: str) -> str:
    """Copy of ``text`` with clearly negative disclaimer spans removed, for the treatment/diagnosis scan only.

    Pure: the caller's text (and so the model output shown to users) is never modified.
    """
    return _DENIAL.sub(" ", text)
_SAFE_CLAIM = re.compile(r"\b(clinically safe|safe regimen|safe to (?:take|use|continue)|is safe|are safe)\b", re.I)
_NEGATION = re.compile(r"\b(not|no|cannot|can't|n't)\b", re.I)


def build_model_input(snapshot: Snapshot, analysis: Analysis, note_context: str | None, terms: Terminology) -> dict:
    """Everything the model may see. Built only after deterministic results exist.

    Evidence text is scoped per finding to that finding's own medications (see evidence_scope); the full catalog is
    untouched and still shown to users as provenance.
    """
    findings = [
        {
            "ruleId": f.rule_id,
            "type": f.type,
            "severity": f.severity,
            "title": f.title,
            "finding": f.risk,
            "evidenceIds": f.source.evidence_ids,
            "observedMedications": [m.model_dump(by_alias=True) for m in f.evidence.medications],
            "observedLabs": [l.model_dump(by_alias=True) for l in f.evidence.labs],
            "trigger": f.provenance.trigger.model_dump(by_alias=True) if f.provenance.trigger else None,
            "evidence": scoped_evidence(f.provenance.evidence, [m.name for m in f.evidence.medications], terms),
        }
        for f in analysis.findings
    ]
    gaps = [
        {
            "ruleId": g.rule_id, "title": g.title, "status": g.status, "finding": g.finding,
            "medication": g.medication.model_dump(by_alias=True),
            "requiredLab": g.required_lab.model_dump(by_alias=True), "lookbackDays": g.lookback_days,
        }
        for g in analysis.data_gaps
    ]
    return {
        "patientId": analysis.patient_id,
        "structuredData": snapshot.model_dump(by_alias=True, mode="json", exclude_none=True),
        "overallSeverity": analysis.overall_severity,
        "summaryCounts": analysis.summary.model_dump(by_alias=True),
        "deterministicFindings": findings,
        "dataGaps": gaps,
        "unstructuredContext": note_context,
    }


def _vocabulary(terms: Terminology) -> dict[str, set[str]]:
    """canonical name -> lowercase surface forms searched for in text."""
    vocab = {}
    for name in [*terms.drugs.values(), *(t.name for t in terms.labs.values())]:
        low = name.lower()
        vocab[name] = {low, low.split()[0]} if " " in low else {low}
    return vocab


def _mentions(text: str, forms: set[str]) -> bool:
    return any(re.search(rf"\b{re.escape(f)}\b", text, re.I) for f in forms)


def validate_grounding(payload: dict, model_input: dict, terms: Terminology) -> None:
    problems: list[str] = []
    flagged: list[dict] = []  # informational: the sentence behind each content category (decisions use `problems`)

    # ---- shape ------------------------------------------------------------------------------
    summary = payload.get("summary")
    explanations = payload.get("findingExplanations")
    gap_text = payload.get("dataGapExplanation")
    if not isinstance(summary, str) or not summary.strip():
        problems.append("summary missing")
    if not isinstance(explanations, list) or not all(
        isinstance(e, dict) and isinstance(e.get("ruleId"), str) and isinstance(e.get("explanation"), str)
        for e in explanations or []
    ):
        raise GroundingViolation("findingExplanations malformed")
    if payload.get("groundedInFindingsOnly") is not True:
        problems.append("groundedInFindingsOnly is not true")

    supplied = {f["ruleId"]: f for f in model_input["deterministicFindings"]}
    gap_ids = {g["ruleId"] for g in model_input["dataGaps"]}
    got = [e["ruleId"] for e in explanations]
    if sorted(got) != sorted(supplied):
        problems.append(f"finding rule IDs {sorted(got)} != supplied {sorted(supplied)}")
    if gap_ids and not (isinstance(gap_text, str) and gap_text.strip()):
        problems.append("data gap present but not explained")
    if not gap_ids and gap_text:
        problems.append("data gap explained but none was supplied")

    # ---- per-text checks --------------------------------------------------------------------
    overall_allowed = {f["severity"] for f in supplied.values()} | {model_input["overallSeverity"]}
    texts: list[tuple[str, set[str]]] = [(summary or "", overall_allowed)]
    texts += [(e["explanation"], {supplied[e["ruleId"]]["severity"]} if e["ruleId"] in supplied else set())
              for e in explanations]
    if isinstance(gap_text, str):
        texts.append((gap_text, set()))

    allowed_ids = set(supplied) | gap_ids
    # Notes are context only: numbers that appear solely in note text are NOT grounded, so a note
    # can never smuggle in a lab value or dose that the structured record does not contain.
    structured = {k: v for k, v in model_input.items() if k != "unstructuredContext"}
    allowed_numbers = {float(n) for n in _NUMBER.findall(json.dumps(structured))}
    # Names this patient's own data mentions (evidence summaries are excluded: they name other drugs).
    context = json.dumps(model_input["structuredData"]) + json.dumps(model_input["dataGaps"]) + json.dumps(
        [(f["observedMedications"], f["observedLabs"]) for f in supplied.values()])
    present = {n for n, forms in _vocabulary(terms).items() if _mentions(context, forms)}

    for text, allowed_sev in texts:
        for rid in _RULE_ID.findall(text):
            if rid not in allowed_ids:
                problems.append(f"unsupplied rule ID {rid}")
        for sev in _UPPER_SEVERITY.findall(text):
            if sev not in allowed_sev:
                problems.append(f"severity {sev} not supplied for this text")
        for sev in _PHRASE_SEVERITY.findall(text):
            if sev.upper() not in allowed_sev:
                problems.append(f"severity phrase '{sev}' not supplied for this text")
        for n in _NUMBER.findall(text):
            if float(n) not in allowed_numbers:
                problems.append(f"number {n} not in supplied data")
        for name, forms in _vocabulary(terms).items():
            if name not in present and _mentions(text, forms):
                problems.append(f"'{name}' is not part of this patient's supplied data")
        scan = strip_denials(text)  # disclaimers removed for these two scans ONLY; `text` itself is untouched
        if _TREATMENT.search(scan) or _ADVICE.search(scan) or _SUBSTITUTION.search(scan):
            problems.append("treatment/dosing language")
        if _DIAGNOSIS.search(scan) or _DIAGNOSIS_CLAIM.search(scan):
            problems.append("diagnosis language")
        for sentence in re.split(r"(?<=[.!?])\s+", text):
            plain = strip_denials(sentence)
            if _TREATMENT.search(plain) or _ADVICE.search(plain) or _SUBSTITUTION.search(plain):
                flagged.append({"category": "treatment/dosing language", "sentence": sentence.strip()[:300]})
            if _DIAGNOSIS.search(plain) or _DIAGNOSIS_CLAIM.search(plain):
                flagged.append({"category": "diagnosis language", "sentence": sentence.strip()[:300]})
            if _SAFE_CLAIM.search(sentence) and not _NEGATION.search(sentence):
                problems.append("claims the regimen is safe")
                flagged.append({"category": "claims the regimen is safe", "sentence": sentence.strip()[:300]})

    if problems:
        raise GroundingViolation("; ".join(dict.fromkeys(problems)), flagged=flagged)
