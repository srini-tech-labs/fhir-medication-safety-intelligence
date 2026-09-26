"""No-key explanation: plain-language text assembled only from the supplied deterministic result."""
from __future__ import annotations

from app.models.contract import (
    AIExplanation, Analysis, DataGap, Finding, FindingExplanation, Snapshot,
)
from app.services.explanation.base import ExplanationService
from app.services.explanation.failures import public_reason

_FLAG = {"H": "flagged high", "L": "flagged low", "N": "flagged normal"}
_OP_WORDS = {">": "above", ">=": "at or above", "<": "below", "<=": "at or below"}


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


class MockExplanationService(ExplanationService):
    def explain(
        self,
        patient_snapshot: Snapshot,
        deterministic_analysis: Analysis,
        note_context: str | None = None,
        *,
        fallback_code: str | None = None,
    ) -> AIExplanation:
        a = deterministic_analysis
        explanations = [FindingExplanation(rule_id=f.rule_id, explanation=self._finding(f)) for f in a.findings]
        gap_text = " ".join(self._gap(g) for g in a.data_gaps) or None
        summary = self._summary(a, patient_snapshot, note_context)
        parts = [summary, *(e.explanation for e in explanations)] + ([gap_text] if gap_text else [])
        return AIExplanation(
            text="\n\n".join(parts),
            summary=summary,
            finding_explanations=explanations,
            data_gap_explanation=gap_text,
            grounded_in_findings_only=True,
            mode="mock",
            fallback_code=fallback_code,
            fallback_reason=public_reason(fallback_code) if fallback_code else None,
        )

    # ---- pieces ------------------------------------------------------------------------------
    def _summary(self, a: Analysis, snap: Snapshot, note_context: str | None) -> str:
        s = a.summary
        if not a.findings and not a.data_gaps:
            text = ("No configured rule fired for this patient's structured medications and labs. "
                    "This is not a statement that the regimen is clinically safe; the prototype checks "
                    "only a small, curated rule set.")
        else:
            bits = []
            if a.findings:
                counts = [f"{n} {label}" for n, label in
                          ((s.high, "HIGH"), (s.moderate, "MODERATE"), (s.low, "LOW")) if n]
                bits.append(f"The deterministic rule engine produced {s.total_findings} "
                            f"finding{'s' if s.total_findings != 1 else ''} ({_join(counts)}) "
                            f"with an overall result of {a.overall_severity}.")
            if a.data_gaps:
                bits.append(
                    f"It also reported {s.data_gaps} data gap{'s' if s.data_gaps != 1 else ''}: "
                    "a configured check could not be completed because required data are missing."
                    if a.findings else
                    "The rule engine could not complete a configured check because required data are "
                    "missing (overall status NEEDS_DATA). No clinical finding was produced."
                )
            bits.append("This explanation restates those results only; it does not add findings or change severity.")
            text = " ".join(bits)
        if note_context and snap.documents:
            kinds = _join(sorted({d.type for d in snap.documents}))
            text += (f" A clinical note ({kinds}) is on file and was treated as context only; "
                     "it did not create or change any finding.")
        return text

    def _finding(self, f: Finding) -> str:
        meds = [f"{m.name} (RxNorm {m.rx_cui}{', ' + m.dosage if m.dosage else ''})" for m in f.evidence.medications]
        observed = f"Observed data: the structured record lists an active order for {_join(meds)}."
        if f.evidence.labs:
            labs = [
                f"{l.name} {l.value:g} {l.unit}"
                + (f" on {l.effective_date}" if l.effective_date else "")
                + (f" ({_FLAG[l.interpretation]})" if l.interpretation in _FLAG else "")
                for l in f.evidence.labs
            ]
            observed = (f"Observed data: the structured record lists an active order for {_join(meds)} "
                        f"and a result of {_join(labs)}.")
        elif len(meds) > 1:
            observed = f"Observed data: the structured record lists active orders for {_join(meds)}."

        t = f.provenance.trigger
        rule = f"Rule interpretation: rule {f.rule_id} classifies this as {f.severity}"
        if t:
            rule += f" because the result is {_OP_WORDS[t.operator]} the configured threshold of {t.threshold:g} {t.unit}"
        rule += f". {f.risk}"
        cite = ", ".join(f"{e.evidence_id} ({e.source})" for e in f.provenance.evidence)
        return f"{observed} {rule} Supporting evidence: {cite}."

    def _gap(self, g: DataGap) -> str:
        return (
            f"Rule {g.rule_id} requires a {g.required_lab.name} result (LOINC {g.required_lab.loinc}) within the "
            f"last {g.lookback_days} days for patients with an active {g.medication.name} order "
            f"(RxNorm {g.medication.rx_cui}). None is available in the structured record, so that check could "
            "not be completed. No value has been estimated or assumed."
        )
