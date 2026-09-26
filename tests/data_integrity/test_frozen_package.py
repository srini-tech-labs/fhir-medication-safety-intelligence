"""The Phase 0 package is frozen: verify it is byte-identical to its published checksums."""
from __future__ import annotations

import hashlib

from tests.conftest import PACKAGE_DIR


def test_every_file_matches_sha256sums():
    lines = (PACKAGE_DIR / "SHA256SUMS.txt").read_text().splitlines()
    assert len(lines) >= 49
    for line in lines:
        digest, name = line.split(maxsplit=1)
        actual = hashlib.sha256((PACKAGE_DIR / name.strip().lstrip("*")).read_bytes()).hexdigest()
        assert actual == digest, f"{name} changed"


def test_dataset_shape():
    from tests.conftest import load_json
    m = load_json(PACKAGE_DIR / "manifest.json")
    assert (m["patientCount"], m["goldenCases"]) == (10, 10)
    assert m["clinicalRuleCounts"] == {"drugDrug": 3, "drugLab": 4, "dataGap": 1}
