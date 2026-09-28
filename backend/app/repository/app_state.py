"""Non-FHIR application state for the HealthLake-backed repository (saved analyses, rejected-explanation captures).

These are application artefacts, not clinical data, so they are NOT written into HealthLake (decision recorded for Phase 3):
they use the same local output layout as ``LocalFHIRRepository`` (``data/output``). The later Lambda phase can swap in a
different store behind the same five methods.
"""
from __future__ import annotations

import json
import re
from pathlib import Path


class AppStateStore:
    def __init__(self, output_dir: Path):
        self._dir = output_dir

    def _write(self, key: str, document: dict) -> None:
        path = self._dir / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document, indent=2), "utf-8")

    def _read(self, key: str) -> dict | None:
        path = self._dir / key
        return json.loads(path.read_text("utf-8")) if path.is_file() else None

    def _analysis_keys(self, patient_id: str) -> list[str]:
        directory = self._dir / "analyses" / patient_id
        return sorted(f"analyses/{patient_id}/{p.name}" for p in directory.glob("*.json")) if directory.is_dir() else []

    def save_analysis(self, patient_id: str, analysis_id: str, analysis: dict) -> None:
        self._write(f"analyses/{patient_id}/{analysis_id}.json", analysis)

    def get_analysis(self, patient_id: str, analysis_id: str) -> dict | None:
        # the id becomes part of a storage key: accept only this patient's own id shape (no path tricks)
        if not re.fullmatch(rf"AN-{re.escape(patient_id)}-\d{{3,}}", analysis_id):
            return None
        return self._read(f"analyses/{patient_id}/{analysis_id}.json")

    def get_latest_analysis(self, patient_id: str) -> dict | None:
        keys = self._analysis_keys(patient_id)
        return self._read(keys[-1]) if keys else None

    def next_analysis_number(self, patient_id: str) -> int:
        return len(self._analysis_keys(patient_id)) + 1

    def save_rejected_explanation(self, record: dict) -> None:
        stamp = re.sub(r"[^0-9A-Za-z]", "", record["capturedAt"])
        self._write(f"rejected_explanations/{record['analysisId']}-{stamp}.json", record)

    def ping(self) -> None:
        """Local disk has no separate reachability state; always ready (matches ``ClinicalRepository.ping_app_state``)."""
