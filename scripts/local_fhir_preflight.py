#!/usr/bin/env python
"""Free, offline FHIR R4 preflight of the frozen bulk NDJSON, BEFORE any AWS spend.

Dev-only tool: needs `fhir.resources==6.4.0` (FHIR R4 4.0.1, pydantic v1), which is deliberately NOT a project
dependency. Run it from a throw-away venv:

    uv venv /tmp/fhirvenv && uv pip install --python /tmp/fhirvenv/bin/python "fhir.resources==6.4.0" "pydantic<2"
    /tmp/fhirvenv/bin/python scripts/local_fhir_preflight.py

Checks (read-only; the frozen files are never modified):
  * every line is one JSON object with a resourceType; LF line endings, no BOM;
  * each resource parses as valid FHIR R4 (structure, cardinality, data types);
  * ids match the FHIR id pattern and are unique per type;
  * every Reference resolves inside the dataset; DocumentReference attachment URLs point at an existing Binary;
  * every Binary base64-decodes and equals the frozen note text in documents/*.txt.
This approximates HealthLake's `strict` validation (R4 spec, no profiles) but cannot replicate terminology-binding or
server-side invariant checks; HealthLake remains the authority.

Known library limitation (reported as a WATCH item, not a failure): `fhir.resources` types `Attachment.url` as an absolute
URL, but FHIR R4 defines `url` as `\\S*`, so the standard relative form `Binary/<id>` is legal. Those resources are validated
again with an absolute stand-in URL, and the relative form is checked against the R4 `url` pattern. The frozen data is
never edited.
"""
from __future__ import annotations

import base64
import json
import re
import sys
from pathlib import Path

from fhir.resources import construct_fhir_element

PACKAGE = Path(__file__).resolve().parents[1] / "data" / "phase0_v1_0"
BULK = PACKAGE / "fhir" / "bulk"
ID_RE = re.compile(r"^[A-Za-z0-9\-.]{1,64}$")
URL_R4 = re.compile(r"^\S*$")  # FHIR R4 primitive `url`
ORDER = ["Patient", "Encounter", "MedicationRequest", "Observation", "DocumentReference", "Binary"]


def references(node, found=None):
    found = [] if found is None else found
    if isinstance(node, dict):
        ref = node.get("reference")
        if isinstance(ref, str):
            found.append(ref)
        for value in node.values():
            references(value, found)
    elif isinstance(node, list):
        for value in node:
            references(value, found)
    return found


def main() -> int:
    errors: list[str] = []
    watch: list[str] = []
    resources: dict[tuple[str, str], dict] = {}
    counts: dict[str, int] = {}

    for rtype in ORDER:
        path = BULK / f"{rtype}.ndjson"
        raw = path.read_bytes()
        if raw.startswith(b"\xef\xbb\xbf"):
            errors.append(f"{path.name}: UTF-8 BOM")
        if b"\r\n" in raw:
            errors.append(f"{path.name}: CRLF line endings")
        for n, line in enumerate(raw.decode("utf-8").splitlines(), 1):
            where = f"{path.name}:{n}"
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                errors.append(f"{where}: invalid JSON ({exc})")
                continue
            if obj.get("resourceType") != rtype:
                errors.append(f"{where}: resourceType {obj.get('resourceType')!r} in {rtype}.ndjson")
                continue
            try:
                construct_fhir_element(rtype, obj)  # raises pydantic.ValidationError on R4 violations
            except Exception as exc:  # noqa: BLE001
                if rtype == "DocumentReference" and "attachment -> url" in str(exc) and "URL scheme" in str(exc):
                    watch.append(f"{where} ({obj.get('id')}): relative Attachment.url "
                                 f"{[c['attachment']['url'] for c in obj['content']]} (legal in FHIR R4; library wants a scheme)")
                    stand_in = json.loads(line)
                    for c in stand_in["content"]:
                        if not URL_R4.match(c["attachment"]["url"]):
                            errors.append(f"{where}: attachment url violates the FHIR R4 `url` pattern")
                        c["attachment"]["url"] = "http://example.invalid/" + c["attachment"]["url"]
                    try:
                        construct_fhir_element(rtype, stand_in)  # everything ELSE about the resource must still be valid
                    except Exception as exc2:  # noqa: BLE001
                        errors.append(f"{where} ({obj.get('id')}): {str(exc2).splitlines()[0:4]}")
                else:
                    errors.append(f"{where} ({obj.get('id')}): {str(exc).splitlines()[0:4]}")
            rid = obj.get("id", "")
            if not ID_RE.match(rid):
                errors.append(f"{where}: bad id {rid!r}")
            if (rtype, rid) in resources:
                errors.append(f"{where}: duplicate {rtype}/{rid}")
            resources[(rtype, rid)] = obj
            counts[rtype] = counts.get(rtype, 0) + 1

    for (rtype, rid), obj in resources.items():
        for ref in references(obj):
            if ref.startswith("#") or ref.startswith("http"):
                continue
            target = tuple(ref.split("/", 1))
            if len(target) != 2 or target not in resources:
                errors.append(f"{rtype}/{rid}: unresolved reference {ref!r}")
        if rtype == "DocumentReference":
            for content in obj.get("content", []):
                url = content["attachment"]["url"]
                if tuple(url.split("/", 1)) not in resources:
                    errors.append(f"{rtype}/{rid}: attachment {url!r} has no Binary")

    notes = {p.read_text("utf-8").strip() for p in (PACKAGE / "documents").glob("*.txt")}
    for (rtype, rid), obj in resources.items():
        if rtype == "Binary":
            text = base64.b64decode(obj["data"]).decode("utf-8").strip()
            if text not in notes:
                errors.append(f"Binary/{rid}: decoded text does not match any frozen note in documents/")

    total = sum(counts.values())
    print(f"resources checked: {total}  {counts}")
    print(f"expected: 53 (Patient 10, Encounter 10, MedicationRequest 14, Observation 11, DocumentReference 4, Binary 4)")
    for w in watch:
        print("WATCH:", w)
    if errors:
        print(f"\nFAILED: {len(errors)} problem(s)")
        for e in errors:
            print("  -", e)
        return 1
    print("PASSED: all resources are valid FHIR R4 (structure/datatypes), ids unique, references and Binary notes consistent"
          + (f" ({len(watch)} watch item(s): see above)" if watch else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
