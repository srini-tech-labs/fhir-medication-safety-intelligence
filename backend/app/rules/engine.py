"""Deterministic medication-safety engine.

Steps (handoff section 15, 1-8): active medications -> relevant labs -> normalized identifiers ->
drug-drug rules -> drug-lab rules -> missing-data rules -> overall result -> structured findings.
No AI and no unstructured text is involved; identity is RxCUI / LOINC only.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from app.models.clinical import LabResult, MedicationOrder
from app.models.contract import (
    AnalysisSummary, DataGap, EvidenceLab, EvidenceMedication, Finding, FindingEvidence,
    FindingSource, OverallStatus, Provenance, RequiredLab, Trigger,
)
from app.rules.catalog import OPERATORS, RuleCatalog
from app.rules.severity import SEVERITY_RANK, overall_status


@dataclass(frozen=True)
class EngineResult:
    findings: list[Finding]
    data_gaps: list[DataGap]
    overall: OverallStatus
    summary: AnalysisSummary


class RuleEngine:
    def __init__(self, catalog: RuleCatalog):
        self._catalog = catalog

    def evaluate(
        self,
        patient_id: str,
        medications: list[MedicationOrder],
        labs: list[LabResult],
        as_of: date,
    ) -> EngineResult:
        # 1. active medications, keyed by RxCUI
        active = {m.rx_cui: m for m in medications if m.status == "active" and m.rx_cui}
        # 2-3. most recent numeric result per LOINC code
        latest: dict[str, LabResult] = {}
        for lab in labs:
            if lab.loinc and (lab.loinc not in latest or lab.effective_date >= latest[lab.loinc].effective_date):
                latest[lab.loinc] = lab

        raw: list[Finding] = []  # catalog order: drug-drug rules, then drug-lab rules
        raw += self._drug_drug(active)  # 4
        raw += self._drug_lab(active, latest)  # 5
        gaps = self._data_gaps(patient_id, active, labs, as_of)  # 6

        # 8. order by severity (HIGH first), stable within a severity; number after ordering
        ordered = sorted(raw, key=lambda f: -SEVERITY_RANK[f.severity])
        findings = [f.model_copy(update={"finding_id": f"F-{patient_id}-{n:03d}"}) for n, f in enumerate(ordered, 1)]

        # 7. overall result
        overall = overall_status([f.severity for f in findings], len(gaps))
        summary = AnalysisSummary(
            total_findings=len(findings),
            high=sum(f.severity == "HIGH" for f in findings),
            moderate=sum(f.severity == "MODERATE" for f in findings),
            low=sum(f.severity == "LOW" for f in findings),
            data_gaps=len(gaps),
        )
        return EngineResult(findings, gaps, overall, summary)

    # ---- drug-drug --------------------------------------------------------------------------
    def _drug_drug(self, active: dict[str, MedicationOrder]) -> list[Finding]:
        out = []
        for rule in self._catalog.drug_drug:
            a, b = active.get(rule.drug_a.rxcui), active.get(rule.drug_b.rxcui)
            if a and b:
                out.append(self._finding(
                    rule, "DRUG_DRUG", meds=[a, b], labs=[], trigger=None,
                ))
        return out

    # ---- drug-lab ---------------------------------------------------------------------------
    def _drug_lab(self, active: dict[str, MedicationOrder], latest: dict[str, LabResult]) -> list[Finding]:
        out = []
        for rule in self._catalog.drug_lab:
            med, lab = active.get(rule.drug.rxcui), latest.get(rule.lab_loinc)
            if not med or not lab:
                continue
            if lab.unit != rule.lab_unit:
                continue  # never silently convert units; a mismatched result is not evaluated
            if OPERATORS[rule.operator](lab.value, rule.threshold):
                trigger = Trigger(operator=rule.operator, threshold=rule.threshold,
                                  unit=rule.lab_unit, note=rule.threshold_note)
                out.append(self._finding(rule, "DRUG_LAB", meds=[med], labs=[lab], trigger=trigger))
        return out

    # ---- missing data -----------------------------------------------------------------------
    def _data_gaps(self, patient_id: str, active: dict[str, MedicationOrder],
                   labs: list[LabResult], as_of: date) -> list[DataGap]:
        gaps = []
        for rule in self._catalog.data_gap:
            med = active.get(rule.drug.rxcui)
            if not med:
                continue
            recent = any(
                l.loinc == rule.lab_loinc and (as_of - l.effective_date).days <= rule.lookback_days
                for l in labs
            )
            if recent:
                continue
            gaps.append(DataGap(
                gap_id=f"G-{patient_id}-{len(gaps) + 1:03d}",
                rule_id=rule.rule_id,
                title=rule.title,
                finding=rule.finding,
                medication=_med(med),
                required_lab=RequiredLab(name=rule.lab_name, loinc=rule.lab_loinc),
                lookback_days=rule.lookback_days,
                source=self._source(rule.evidence_ids),
                provenance=Provenance(
                    rule_version=self._catalog.version,
                    fhir_resources=[med.fhir_ref],
                    evidence=[self._catalog.evidence[e] for e in rule.evidence_ids],
                ),
            ))
        return gaps

    # ---- builders ---------------------------------------------------------------------------
    def _source(self, evidence_ids: tuple[str, ...]) -> FindingSource:
        primary = self._catalog.evidence[evidence_ids[0]]
        return FindingSource(type=primary.source_type, provider=primary.provider,
                             evidence_ids=list(evidence_ids))

    def _finding(self, rule, rule_type: str, meds: list[MedicationOrder], labs: list[LabResult],
                 trigger: Trigger | None) -> Finding:
        return Finding(
            finding_id="",  # assigned after severity ordering
            rule_id=rule.rule_id,
            type=rule_type,
            severity=rule.severity,
            title=rule.title,
            evidence=FindingEvidence(medications=[_med(m) for m in meds], labs=[_lab(l) for l in labs]),
            risk=rule.finding,
            source=self._source(rule.evidence_ids),
            provenance=Provenance(
                rule_version=self._catalog.version,
                trigger=trigger,
                fhir_resources=[m.fhir_ref for m in meds] + [l.fhir_ref for l in labs],
                evidence=[self._catalog.evidence[e] for e in rule.evidence_ids],
                severity_disclaimer=self._catalog.severity_disclaimer,
            ),
        )


def _med(m: MedicationOrder) -> EvidenceMedication:
    return EvidenceMedication(name=m.name, rx_cui=m.rx_cui or "", dosage=m.dosage)


def _lab(l: LabResult) -> EvidenceLab:
    return EvidenceLab(
        name=l.name, loinc=l.loinc or "", value=l.value, unit=l.unit or "",
        interpretation=l.interpretation, effective_date=l.effective_date.isoformat(),
    )
