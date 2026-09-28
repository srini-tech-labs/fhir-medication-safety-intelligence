"""RxNorm / LOINC lookup tables loaded from the frozen package (terminology/*.json)."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

RXNORM_SYSTEM = "http://www.nlm.nih.gov/research/umls/rxnorm"
LOINC_SYSTEM = "http://loinc.org"


@dataclass(frozen=True)
class LabTerm:
    name: str
    code: str
    display: str
    ucum: str


@dataclass(frozen=True)
class Terminology:
    drugs: dict[str, str]  # RxCUI -> ingredient name
    labs: dict[str, LabTerm]  # LOINC -> lab term

    @classmethod
    def load(cls, package_dir: Path) -> "Terminology":
        rx = json.loads((package_dir / "terminology" / "rxnorm_mapping.json").read_text("utf-8"))
        lo = json.loads((package_dir / "terminology" / "loinc_mapping.json").read_text("utf-8"))
        return cls(
            drugs={m["rxcui"]: m["name"] for m in rx["medications"]},
            labs={
                x["code"]: LabTerm(x["name"], x["code"], x["display"], x["ucum"])
                for x in lo["labs"]
            },
        )
