"""FHIR R4 bundle -> internal clinical objects.

Pure functions, independent of where the bundle came from (local file, S3, ...).
Matching identity is always the RxNorm / LOINC *code*, never display text.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import date

from app.models.clinical import ClinicalDocument, LabResult, MedicationOrder, PatientRecord
from app.terminology import LOINC_SYSTEM, RXNORM_SYSTEM, Terminology


@dataclass(frozen=True)
class ParsedBundle:
    patient: PatientRecord
    medications: list[MedicationOrder]
    labs: list[LabResult]
    documents: list[ClinicalDocument]


def _ref(resource: dict) -> str:
    return f"{resource['resourceType']}/{resource['id']}"


def _coding(concept: dict, system: str) -> dict | None:
    return next((c for c in concept.get("coding", []) if c.get("system") == system), None)


def _parse_date(value: str | None) -> date | None:
    return date.fromisoformat(value[:10]) if value else None


def _patient_app_id(patient: dict) -> str:
    return next(i["value"] for i in patient["identifier"] if i["system"].endswith("synthetic-patient-id"))


def _medication(res: dict, terms: Terminology) -> MedicationOrder:
    concept = res["medicationCodeableConcept"]
    coding = _coding(concept, RXNORM_SYSTEM)
    rx_cui = coding["code"] if coding else None
    name = terms.drugs.get(rx_cui or "") or concept.get("text") or (coding or {}).get("display") or "Unknown"
    dosage = next((d["text"] for d in res.get("dosageInstruction", []) if d.get("text")), None)
    return MedicationOrder(_ref(res), name, rx_cui, dosage, res["status"])


def _lab(res: dict, terms: Terminology) -> LabResult | None:
    qty = res.get("valueQuantity")
    if qty is None:
        return None  # only numeric quantities are used by the rule set
    coding = _coding(res["code"], LOINC_SYSTEM)
    loinc = coding["code"] if coding else None
    term = terms.labs.get(loinc or "")
    interp = next((c["code"] for i in res.get("interpretation", []) for c in i.get("coding", [])), None)
    rng = (res.get("referenceRange") or [{}])[0]
    return LabResult(
        fhir_ref=_ref(res),
        name=term.name if term else res["code"].get("text", "Unknown"),
        loinc=loinc,
        value=float(qty["value"]),
        unit=qty.get("code") or qty.get("unit"),
        interpretation=interp,
        effective_date=_parse_date(res["effectiveDateTime"]),
        ref_low=(rng.get("low") or {}).get("value"),
        ref_high=(rng.get("high") or {}).get("value"),
    )


def _documents(patient_id: str, resources: list[dict]) -> list[ClinicalDocument]:
    binaries = {r["id"]: r for r in resources if r["resourceType"] == "Binary"}
    refs = sorted(
        (r for r in resources if r["resourceType"] == "DocumentReference"),
        key=lambda r: r.get("date", ""),
    )
    docs: list[ClinicalDocument] = []
    for n, ref in enumerate(refs, start=1):
        attachment = ref["content"][0]["attachment"]
        binary = binaries.get(attachment["url"].split("/", 1)[1])
        if binary is None:
            continue
        text = base64.b64decode(binary["data"]).decode("utf-8")
        docs.append(
            ClinicalDocument(
                id=f"DOC-{patient_id}-{n:02d}",
                fhir_ref=_ref(ref),
                type=ref["type"]["text"],
                title=attachment.get("title"),
                date=_parse_date(ref.get("date")),
                text=text,
            )
        )
    return docs


def parse_bundle(bundle: dict, terms: Terminology) -> ParsedBundle:
    resources = [e["resource"] for e in bundle["entry"]]
    by_type: dict[str, list[dict]] = {}
    for r in resources:
        by_type.setdefault(r["resourceType"], []).append(r)

    p = by_type["Patient"][0]
    patient_id = _patient_app_id(p)
    scenario = None
    for enc in by_type.get("Encounter", []):
        reasons = enc.get("reasonCode") or []
        if reasons:
            scenario = reasons[0].get("text")
            break
    patient = PatientRecord(
        id=patient_id,
        fhir_ref=_ref(p),
        name=p["name"][0]["text"],
        sex=p["gender"],
        birth_date=date.fromisoformat(p["birthDate"]),
        scenario_label=scenario,
    )
    labs = [x for r in by_type.get("Observation", []) if (x := _lab(r, terms)) is not None]
    return ParsedBundle(
        patient=patient,
        medications=[_medication(r, terms) for r in by_type.get("MedicationRequest", [])],
        labs=labs,
        documents=_documents(patient_id, resources),
    )
