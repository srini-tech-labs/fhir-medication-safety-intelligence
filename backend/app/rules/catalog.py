"""Curated rule + evidence catalog, loaded verbatim from the frozen package (rules/*.json).

The engine is data-driven: rules are never hard-coded, so the frozen JSON is the single source of
truth for combinations, thresholds and severities.
"""
from __future__ import annotations

import json
import operator as op
from dataclasses import dataclass
from pathlib import Path

from app.models.contract import EvidenceReference

OPERATORS = {">": op.gt, ">=": op.ge, "<": op.lt, "<=": op.le}


@dataclass(frozen=True)
class DrugRef:
    name: str
    rxcui: str


@dataclass(frozen=True)
class DrugDrugRule:
    rule_id: str
    drug_a: DrugRef
    drug_b: DrugRef
    severity: str
    title: str
    finding: str
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class DrugLabRule:
    rule_id: str
    drug: DrugRef
    lab_name: str
    lab_loinc: str
    lab_unit: str
    operator: str
    threshold: float
    severity: str
    title: str
    finding: str
    evidence_ids: tuple[str, ...]
    threshold_note: str | None


@dataclass(frozen=True)
class DataGapRule:
    rule_id: str
    drug: DrugRef
    lab_name: str
    lab_loinc: str
    lookback_days: int
    title: str
    finding: str
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class RuleCatalog:
    version: str
    severity_disclaimer: str
    drug_drug: tuple[DrugDrugRule, ...]
    drug_lab: tuple[DrugLabRule, ...]
    data_gap: tuple[DataGapRule, ...]
    evidence: dict[str, EvidenceReference]

    @classmethod
    def load(cls, package_dir: Path) -> "RuleCatalog":
        def read(name: str) -> dict:
            return json.loads((package_dir / "rules" / name).read_text("utf-8"))

        ddi, dl, dg, ev = (
            read("drug_drug_rules.json"),
            read("drug_lab_rules.json"),
            read("data_gap_rules.json"),
            read("rule_evidence.json"),
        )
        if not (ddi["version"] == dl["version"] == dg["version"] == ev["version"]):
            raise ValueError("Rule files carry mismatched versions")

        def drug(d: dict) -> DrugRef:
            return DrugRef(d["name"], d["rxcui"])

        evidence = {e["evidenceId"]: _evidence(e) for e in ev["evidence"]}
        catalog = cls(
            version=ddi["version"],
            severity_disclaimer=ddi["severityDisclaimer"],
            drug_drug=tuple(
                DrugDrugRule(
                    r["ruleId"], drug(r["drugA"]), drug(r["drugB"]), r["severity"],
                    r["title"], r["finding"], tuple(r["evidenceIds"]),
                )
                for r in ddi["rules"]
            ),
            drug_lab=tuple(
                DrugLabRule(
                    r["ruleId"], drug(r["drug"]), r["lab"]["name"], r["lab"]["loinc"], r["lab"]["unit"],
                    r["operator"], float(r["threshold"]), r["severity"], r["title"], r["finding"],
                    tuple(r["evidenceIds"]), r.get("thresholdNote"),
                )
                for r in dl["rules"]
            ),
            data_gap=tuple(
                DataGapRule(
                    r["ruleId"], drug(r["drug"]), r["requiredLab"]["name"], r["requiredLab"]["loinc"],
                    int(r["lookbackDays"]), r["title"], r["finding"], tuple(r["evidenceIds"]),
                )
                for r in dg["rules"]
            ),
            evidence=evidence,
        )
        catalog._validate()
        return catalog

    def _validate(self) -> None:
        for rule in (*self.drug_drug, *self.drug_lab):
            if rule.severity not in ("HIGH", "MODERATE", "LOW"):
                raise ValueError(f"{rule.rule_id}: bad severity {rule.severity!r}")
        for rule in self.drug_lab:
            if rule.operator not in OPERATORS:
                raise ValueError(f"{rule.rule_id}: unsupported operator {rule.operator!r}")
        for rule in (*self.drug_drug, *self.drug_lab, *self.data_gap):
            missing = [e for e in rule.evidence_ids if e not in self.evidence]
            if missing:
                raise ValueError(f"{rule.rule_id}: unknown evidence {missing}")


def _evidence(e: dict) -> EvidenceReference:
    # Classify by publisher so the UI can label DailyMed labels vs. guideline sources.
    if e["source"].startswith("DailyMed"):
        source_type, provider = "FDA_DRUG_LABEL", "DailyMed"
    else:
        source_type, provider = "CLINICAL_GUIDELINE", e["source"].split(" Standards")[0]
    return EvidenceReference(
        evidence_id=e["evidenceId"], title=e["title"], source=e["source"], section=e["section"],
        source_url=e["sourceUrl"], summary=e["summary"], accessed=e["accessed"],
        source_type=source_type, provider=provider,
    )
