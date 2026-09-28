"""Phase 1 repository: FHIR bundles from the local Phase 0 package, output under data/output."""
from __future__ import annotations

import json
from pathlib import Path

from app.repository.fhir_bundle_repository import FHIRBundleRepository
from app.terminology import Terminology


class LocalFHIRRepository(FHIRBundleRepository):
    def __init__(self, package_dir: Path, output_dir: Path, terminology: Terminology | None = None):
        super().__init__(terminology or Terminology.load(package_dir))
        self._bundles_dir = package_dir / "fhir" / "bundles"
        self._output_dir = output_dir

    def _bundle_patient_ids(self) -> list[str]:
        return sorted(p.name.split("-")[0] for p in self._bundles_dir.glob("P*-bundle.json"))

    def _read_bundle(self, patient_id: str) -> dict:
        return json.loads((self._bundles_dir / f"{patient_id}-bundle.json").read_text("utf-8"))

    def _write_output(self, key: str, document: dict) -> None:
        path = self._output_dir / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document, indent=2), "utf-8")

    def _read_output(self, key: str) -> dict | None:
        path = self._output_dir / key
        return json.loads(path.read_text("utf-8")) if path.is_file() else None

    def _list_output_keys(self, prefix: str) -> list[str]:
        directory = self._output_dir / prefix
        if not directory.is_dir():
            return []
        return [f"{prefix}{p.name}" for p in directory.glob("*.json")]
