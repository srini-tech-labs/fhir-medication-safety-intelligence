#!/usr/bin/env python
"""Phase 3 direct FHIR verification of an AWS HealthLake datastore (V0-V19), over SigV4.

    backend/.venv/bin/python scripts/healthlake_verify.py --datastore-id <id> \
        --verifier-role-arn arn:aws:iam::<acct>:role/MedSafetyHealthLakeVerifierRole \
        --app-role-arn      arn:aws:iam::<acct>:role/MedSafetyHealthLakeAppRole --out report.json

* Frozen resources (patient-p001 ... patient-p010 and their records) are only ever READ. All writes use temporary synthetic
  resources (ids `zz-phase3-tmp-*`, tag `https://example.org/fhir/CodeSystem/phase3-test|temp`) which V19 deletes.
* V0-V17 and V19 run as the Verifier role; V18 additionally uses the App role and an unsigned request to prove least privilege.
* Search is eventually consistent: any read-after-write check polls (<= 60 s) and reports the measured lag.
* Nothing is assumed: documented-but-unverified behaviours (type-level `_history`, `_summary`+`_elements`, `_revinclude` spelling)
  are recorded as observations, not silently passed.
* Exit code 0 only if every check passed. The report contains request ids, statuses and counts -- no clinical content.
* `--checks` runs a subset (comma-separated ids, e.g. `V0,V1,V2,V3,V4,V5`); V19 (frozen data unchanged) always runs regardless,
  because it is the one check that confirms a fresh import reproduces the frozen dataset. Default: all of V0-V19. A recreation
  (new datastore, re-import) should run the SHORTENED set first for a fast go/no-go, then the full suite before relying on it:
  `--checks V0,V1,V2,V3,V4,V5` = capability statement, one read (+ the Patient/P001 404 shape), identifier search, accurate
  per-type counts, patient-scoped searches, and the Binary note byte-match -- the read-path checks that depend on the import,
  skipping the slower write/history/authorization checks (V6-V18) that Phase 3 already proved and re-import does not change.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "backend"))

import httpx  # noqa: E402

from app.repository.healthlake_client import HealthLakeClient, HealthLakeError, HealthLakeResponse  # noqa: E402

PACKAGE = REPO / "data" / "phase0_v1_0"
SYSTEM = "https://example.org/synthetic-patient-id"
TEMP_TAG = {"system": "https://example.org/fhir/CodeSystem/phase3-test", "code": "temp"}
TEMP_TAG_TOKEN = f"{TEMP_TAG['system']}|{TEMP_TAG['code']}"
TMP = "zz-phase3-tmp-"
EXPECTED_COUNTS = {"Patient": 10, "Encounter": 10, "MedicationRequest": 14, "Observation": 11, "DocumentReference": 4, "Binary": 4}
ORDER = list(EXPECTED_COUNTS)
POLL_SECONDS = 60
HISTORY_POLL_SECONDS = 600  # observed live: version history lags writes by minutes (imports and updates alike)


def frozen_resources() -> dict[tuple[str, str], dict]:
    out = {}
    for rtype in ORDER:
        for line in (PACKAGE / "fhir" / "bulk" / f"{rtype}.ndjson").read_text("utf-8").splitlines():
            r = json.loads(line)
            out[(r["resourceType"], r["id"])] = r
    return out


def no_meta(resource: dict) -> dict:
    r = {k: v for k, v in resource.items() if k != "meta"}
    tags = [t for t in (resource.get("meta") or {}).get("tag", []) if t.get("code") != "SUBSETTED"]
    return {**r, "_tags": sorted(json.dumps(t, sort_keys=True) for t in tags)}


class Fail(AssertionError):
    pass


def expect(cond, message: str):
    if not cond:
        raise Fail(message)


def location_id(location: str) -> str:
    return location.split("Observation/")[1].split("/")[0]


def temp_observation(rid: str | None = None, status: str | None = "final", subject: str = f"Patient/{TMP}patient", **extra) -> dict:
    """Temp Observation. Callers inside a Suite pass ids/subject built from `Suite.tmp` (unique per run: HealthLake keeps tombstones of deleted ids)."""
    r = {"resourceType": "Observation", "meta": {"tag": [TEMP_TAG]}, "code": {"text": "phase3 temp"}, "subject": {"reference": subject},
         "valueString": "temporary", **extra}
    if status:
        r["status"] = status
    if rid:
        r["id"] = rid
    return r


class Suite:
    def __init__(self, verifier: HealthLakeClient, app: HealthLakeClient, raw_get: Callable[[str], httpx.Response],
                 *, poll_seconds: float = POLL_SECONDS, history_poll_seconds: float = HISTORY_POLL_SECONDS,
                 sleep: Callable[[float], None] = time.sleep):
        self.v, self.app, self.raw_get = verifier, app, raw_get
        self.poll_seconds, self.history_poll_seconds, self.sleep = poll_seconds, history_poll_seconds, sleep
        self.frozen = frozen_resources()
        self.tmp = f"{TMP}{uuid.uuid4().hex[:8]}-"  # ids of deleted resources can never be reused (If-None-Match: * -> 412, GET -> 410)
        self.results: list[dict] = []
        self.rids: list[dict] = []
        self.created: set[tuple[str, str]] = set()
        self.current: dict | None = None
        for who, client in (("verifier", self.v), ("app", self.app)):
            self._record(who, client)

    # ---- plumbing ----------------------------------------------------------------------------------------
    def _record(self, who: str, client: HealthLakeClient) -> None:
        original = client.request

        def wrapped(method, path, **kw):
            resp = original(method, path, **kw)
            entry = {"who": who, "method": method, "path": path.split("?")[0][-80:], "status": resp.status, "requestId": resp.request_id}
            self.rids.append(entry)
            if self.current is not None:
                self.current["requests"].append(entry)
            return resp

        client.request = wrapped  # type: ignore[method-assign]

    def eventually(self, fn: Callable[[], bool], *, seconds: float | None = None) -> float:
        """Poll until fn() is true; returns the seconds waited. Raises Fail after the deadline (default: poll_seconds)."""
        limit = self.poll_seconds if seconds is None else seconds
        start = time.monotonic()
        while True:
            if fn():
                return time.monotonic() - start
            if time.monotonic() - start > limit:
                raise Fail(f"not visible within {limit:.0f}s")
            self.sleep(1.0 if limit <= 60 else 10.0)

    def search(self, rtype: str, params, client: HealthLakeClient | None = None) -> list[dict]:
        return [e for e in (client or self.v).search_entries(rtype, params)]

    def total(self, rtype: str, params=()) -> int:
        body = self.v.get_json(rtype, [*params, ("_total", "accurate"), ("_count", "1")])
        return int(body["total"])

    def run(self, vid: str, name: str, fn: Callable[[], dict | None]) -> None:
        self.current = {"id": vid, "name": name, "requests": []}
        started = time.monotonic()
        try:
            detail = fn() or {}
            status = "PASS"
        except Fail as exc:
            detail, status = {"reason": str(exc)}, "FAIL"
        except HealthLakeError as exc:
            detail, status = {"reason": f"{type(exc).__name__}: {exc}", "issues": exc.issues[:5], "requestId": exc.request_id}, "FAIL"
        except Exception as exc:  # noqa: BLE001 - a crashing check is a failed check, never a silent skip
            detail, status = {"reason": f"{type(exc).__name__}: {exc}"}, "FAIL"
        entry = {**self.current, "status": status, "seconds": round(time.monotonic() - started, 2), "detail": detail}
        self.results.append(entry)
        self.current = None
        print(f"{vid:>4} {status:<4} {name}" + ("" if status == "PASS" else f"  -> {detail.get('reason')}"), flush=True)

    # ---- V0-V10 : read-only against the imported data ---------------------------------------------------
    def v0(self):
        r = self.v.request("GET", "metadata").raise_for_status()
        expect(r.body and r.body.get("resourceType") == "CapabilityStatement", "not a CapabilityStatement")
        expect(str(r.body.get("fhirVersion", "")).startswith("4.0"), f"fhirVersion {r.body.get('fhirVersion')}")
        return {"fhirVersion": r.body["fhirVersion"], "requestId": r.request_id}

    def v1(self):
        r = self.v.request("GET", "Patient/patient-p001").raise_for_status()
        p = r.body
        expect(any(i.get("value") == "P001" and i.get("system") == SYSTEM for i in p["identifier"]), "identifier P001 missing")
        expect(p["meta"]["versionId"] == "1" and r.etag == 'W/"1"', f"versionId {p['meta'].get('versionId')} etag {r.etag}")
        nf = self.v.request("GET", "Patient/P001")
        expect(nf.status == 404, f"Patient/P001 returned {nf.status}, documented as 404 (P001 is only an identifier)")
        return {"patient-p001": "200 v1", "Patient/P001": nf.status}

    def v2(self):
        b = self.v.get_json("Patient", [("identifier", f"{SYSTEM}|P001"), ("_total", "accurate")])
        expect(b.get("total") == 1 and len(b["entry"]) == 1, f"total {b.get('total')}")
        expect(b["entry"][0]["resource"]["id"] == "patient-p001", "wrong patient")

    def v3(self):
        totals = {t: self.total(t) for t in EXPECTED_COUNTS}
        totals.update({t: self.total(t) for t in ("DetectedIssue", "RiskAssessment")})
        expected = {**EXPECTED_COUNTS, "DetectedIssue": 0, "RiskAssessment": 0}
        expect(totals == expected, f"{totals} != {expected}")
        b = self.v.get_json("Patient", [("_total", "accurate"), ("_count", "100")])
        expect(len(b["entry"]) == 10, f"{len(b['entry'])} patients on one page")
        return totals

    def v4(self):
        meds = self.search("MedicationRequest", [("patient", "patient-p001")])
        expect(len(meds) == 1 and meds[0]["resource"]["medicationCodeableConcept"]["coding"][0]["code"] == "29046", "lisinopril 29046")
        obs = self.search("Observation", [("patient", "patient-p001")])
        expect(len(obs) == 1, f"{len(obs)} observations")
        o = obs[0]["resource"]
        expect(o["code"]["coding"][0]["code"] == "2823-3" and o["valueQuantity"]["value"] == 5.8 and o["valueQuantity"]["unit"] == "mmol/L", "potassium 5.8 mmol/L")
        docs = self.search("DocumentReference", [("patient", "patient-p002")])
        expect(len(docs) == 1, f"{len(docs)} documents for p002")

    def v5(self):
        (doc,) = [e["resource"] for e in self.search("DocumentReference", [("patient", "patient-p002")])]
        url = doc["content"][0]["attachment"]["url"]
        expect(url.startswith("Binary/"), f"attachment url {url}")
        binary = self.v.read("Binary", url.split("/", 1)[1])
        data = base64.b64decode(binary["data"])
        frozen = base64.b64decode(self.frozen[("Binary", "bin-p002-note")]["data"])
        note = (PACKAGE / "documents" / "P002-progress-note.txt").read_bytes()
        expect(data == frozen, "Binary payload differs from the frozen Binary")
        expect(data.strip() == note.strip(), "Binary payload differs from the frozen note text")
        return {"sha256": hashlib.sha256(data).hexdigest(), "contentType": binary.get("contentType")}

    def v6(self):
        b = self.v.get_json("Observation", [("_id", "obs-p001-01"), ("_include", "Observation:subject")])
        modes = {(e["resource"]["resourceType"], e.get("search", {}).get("mode")) for e in b["entry"]}
        expect(modes == {("Observation", "match"), ("Patient", "include")}, str(modes))
        b = self.v.get_json("MedicationRequest", [("patient", "patient-p001"), ("_include", "MedicationRequest:subject")])
        expect({e["resource"]["resourceType"] for e in b["entry"]} == {"MedicationRequest", "Patient"}, "MedicationRequest include")

    def v7(self):
        used = None
        for spelling in ("patient", "subject"):
            params = [("_id", "patient-p008"), *[("_revinclude", f"{t}:{spelling}") for t in
                      ("MedicationRequest", "Observation", "Encounter", "DocumentReference")]]
            resp = self.v.request("GET", "Patient", params=params)
            if resp.status == 200:
                used = spelling
                break
        expect(used, "neither :patient nor :subject _revinclude spelling was accepted")
        entries = list(self.v.search_entries("Patient", params))
        counts: dict[str, int] = {}
        for e in entries:
            counts[e["resource"]["resourceType"]] = counts.get(e["resource"]["resourceType"], 0) + 1
        expect(counts == {"Patient": 1, "MedicationRequest": 2, "Observation": 2, "Encounter": 1, "DocumentReference": 1}, str(counts))
        return {"spelling": used, "counts": counts}

    def v8(self):
        b = self.v.get_json("Observation", [("patient", "patient-p001"), ("_elements", "code,valueQuantity")])
        for e in b["entry"]:
            r = e["resource"]
            expect("valueQuantity" in r and "code" in r, "requested elements missing")
            expect(not {"interpretation", "referenceRange", "effectiveDateTime", "category"} & set(r), f"extra elements {sorted(r)}")
            expect(any(t.get("code") == "SUBSETTED" for t in r.get("meta", {}).get("tag", [])), "meta.tag SUBSETTED missing")
        both = self.v.request("GET", "Observation", params=[("patient", "patient-p001"), ("_summary", "true"), ("_elements", "code")])
        return {"_summary+_elements status (documented: rejected)": both.status}

    def v9(self):
        seen, pages, url = [], 0, None
        body = self.v.get_json("Patient", [("_count", "3"), ("_sort", "_id")])
        while True:
            pages += 1
            seen += [e["resource"]["id"] for e in body.get("entry", [])]
            url = next((l["url"] for l in body.get("link", []) if l.get("relation") == "next"), None)
            if not url or pages > 10:
                break
            body = self.v.get_json(url)
        expect(len(seen) == 10 and len(set(seen)) == 10 and seen == sorted(seen), f"{len(seen)} ids over {pages} pages: {seen}")
        expect(pages == 4, f"{pages} pages, expected 4")

    def v10(self):
        chained = self.search("Observation", [("subject:Patient.identifier", f"{SYSTEM}|P008")])
        expect(len(chained) == 2, f"chained search returned {len(chained)}")
        has = self.v.get_json("Patient", [("_has:MedicationRequest:patient:status", "active"), ("_total", "accurate"), ("_count", "100")])
        expect(has.get("total") == 10, f"_has total {has.get('total')}")

    # ---- V11-V17 : writes on temporary synthetic data ------------------------------------------------------
    def obs(self, rid: str | None = None, **kw) -> dict:
        return temp_observation(rid, subject=f"Patient/{self.tmp}patient", **kw)

    def setup_temp_patient(self):
        patient = {"resourceType": "Patient", "id": f"{self.tmp}patient", "meta": {"tag": [TEMP_TAG]},
                   "identifier": [{"system": "https://example.org/phase3-test-id", "value": "TMP"}], "name": [{"text": "Phase Three Temp"}],
                   "gender": "unknown"}
        r = self.v.put(patient).raise_for_status()
        self.created.add(("Patient", patient["id"]))
        return r

    def v11(self):
        self.setup_temp_patient()
        r = self.v.request("POST", "Observation", json_body=self.obs()).raise_for_status()
        expect(r.status == 201, f"status {r.status}")
        rid = (r.body or {}).get("id")
        expect(rid and not rid.startswith("obs-"), "no server-assigned id in the response body")
        expect(r.etag == 'W/"1"' and r.body["meta"]["versionId"] == "1", f"etag {r.etag}")
        self.created.add(("Observation", rid))
        # observed (live): HealthLake returns the new id in the body and ETag; it sends no Location header on POST
        return {"serverAssignedId": True, "etag": r.etag, "location_header_present": "location" in r.headers}

    def v12(self):
        rid = f"{self.tmp}obs-put"
        first = self.v.put(self.obs(rid), if_none_match="*").raise_for_status()
        expect(first.status == 201, f"first PUT {first.status}")
        self.created.add(("Observation", rid))
        again = self.v.put(self.obs(rid), if_none_match="*")
        expect(again.status == 412, f"repeat If-None-Match:* -> {again.status}, expected 412")

    def history_versions(self, rtype: str, rid: str) -> int:
        return len(self.v.get_json(f"{rtype}/{rid}/_history").get("entry", []))

    def v13(self):
        rid = f"{self.tmp}obs-put"
        r2 = self.v.put(self.obs(rid, valueString="amended"), if_match='W/"1"').raise_for_status()
        expect(r2.etag == 'W/"2"', f"etag after update {r2.etag}")
        stale = self.v.put(self.obs(rid, valueString="stale"), if_match='W/"1"')
        expect(stale.status == 412, f"stale If-Match -> {stale.status}, expected 412")
        lag = self.eventually(lambda: self.history_versions("Observation", rid) == 2, seconds=self.history_poll_seconds)  # history is eventually consistent
        expect(self.history_versions("Observation", rid) == 2, "history does not show 2 versions")
        v1 = self.v.get_json(f"Observation/{rid}/_history/1")
        expect(v1["valueString"] == "temporary" and v1["meta"]["versionId"] == "1", "vread v1 is not the original")
        type_level = self.v.request("GET", "Observation/_history", params=[("_count", "1")])
        return {"versions": 2, "history_lag_seconds": round(lag, 1), "type-level _history status": type_level.status,
                "type-level _history note": (type_level.issues() or [(type_level.text or "")[:200]])[0] if not type_level.ok else None}

    def v14(self):
        key = str(uuid.uuid4())
        body = self.obs(meta={"tag": [TEMP_TAG, {"system": TEMP_TAG["system"], "code": "idempotency"}]})
        first = self.v.request("POST", "Observation", json_body=body, headers={"x-amz-fhir-idempotency-key": key})
        expect(first.status == 201, f"first POST {first.status}")
        rid = first.body["id"]
        self.created.add(("Observation", rid))
        second = self.v.request("POST", "Observation", json_body=body, headers={"x-amz-fhir-idempotency-key": key})
        expect(second.status == 409, f"duplicate key -> {second.status}, expected 409")
        expect((second.body or {}).get("resourceType") == "OperationOutcome" and any(i.get("code") == "duplicate" for i in second.body["issue"]),
               "409 is not a duplicate OperationOutcome")
        # observed (live): the ORIGINAL resource is identified by the Location header, not returned in the body
        expect(second.headers.get("location", "").endswith(f"Observation/{rid}"), f"Location {second.headers.get('location')} is not the original {rid}")
        tag = f"{TEMP_TAG['system']}|idempotency"
        self.eventually(lambda: len(self.search("Observation", [("_tag", tag)])) >= 1)
        expect(len(self.search("Observation", [("_tag", tag)])) == 1, "duplicate POST created a second resource")
        return {"duplicate": second.status, "original_in_location_header": True}

    def v15(self):
        rid = f"{self.tmp}obs-consistency"
        self.v.put(self.obs(rid)).raise_for_status()
        self.created.add(("Observation", rid))
        immediate = len(self.search("Observation", [("_id", rid)]))
        lag = 0.0 if immediate else self.eventually(lambda: len(self.search("Observation", [("_id", rid)])) == 1)
        strong = self.v.request("GET", "Observation", params=[("_id", f"{self.tmp}obs-strong-probe")],
                                headers={"x-amz-fhir-history-consistency-level": "strong"})
        return {"visible_immediately": bool(immediate), "eventual_lag_seconds": round(lag, 1), "strong-header status": strong.status}

    def v16(self):
        def bundle(kind, entries):
            return {"resourceType": "Bundle", "type": kind, "entry": entries}

        def post(resource, full=None):
            e = {"resource": resource, "request": {"method": "POST", "url": resource["resourceType"]}}
            return {"fullUrl": full, **e} if full else e

        bad = self.obs(status=None)
        batch = self.v.request("POST", "", json_body=bundle("batch", [post(self.obs()), post(bad)])).raise_for_status()
        statuses = [str(e["response"]["status"])[:3] for e in batch.body["entry"]]
        expect(statuses[0] == "201" and statuses[1].startswith("4"), f"batch statuses {statuses}")
        loc = batch.body["entry"][0]["response"].get("location", "")
        if loc:
            self.created.add(("Observation", location_id(loc)))

        a, b = f"urn:uuid:{uuid.uuid4()}", f"urn:uuid:{uuid.uuid4()}"
        tx = self.v.request("POST", "", json_body=bundle("transaction", [
            post(self.obs(), a), post(self.obs(derivedFrom=[{"reference": a}]), b)])).raise_for_status()
        ids = [location_id(str(e["response"]["location"])) for e in tx.body["entry"]]
        self.created.update(("Observation", i) for i in ids)
        second = self.v.read("Observation", ids[1])
        expect(second["derivedFrom"][0]["reference"].endswith(f"Observation/{ids[0]}"), "urn:uuid reference not rewritten")

        ghost = f"{self.tmp}obs-tx-rollback"
        put = {"resource": self.obs(ghost), "request": {"method": "PUT", "url": f"Observation/{ghost}"}}
        atomic = self.v.request("POST", "", json_body=bundle("transaction", [put, post(bad)]))
        expect(not atomic.ok, f"transaction with an invalid entry returned {atomic.status}")
        expect(self.v.request("GET", f"Observation/{ghost}").status in (404, 410), "transaction was not atomic: the valid entry was committed")
        return {"batch statuses": statuses, "transaction ids": len(ids), "invalid transaction status": atomic.status}

    def v17(self):
        r = self.v.request("POST", "Observation", json_body=self.obs(status=None))
        expect(r.status == 400, f"missing required `status` -> {r.status}, expected 400")
        expect(r.body and r.body.get("resourceType") == "OperationOutcome", "no OperationOutcome")
        return {"issues": r.issues()[:3]}

    # ---- V18 : authorization --------------------------------------------------------------------------------
    def v18(self):
        base = self.v.base_url
        unsigned = self.raw_get(base + "Patient/patient-p001")
        expect(unsigned.status_code in (401, 403), f"unsigned request -> {unsigned.status_code}")
        ok = self.app.request("GET", "Patient/patient-p001").raise_for_status()  # first proof of the datastore/fhir/<id> ARN form
        rid = f"{self.tmp}obs-put"
        current = self.v.read("Observation", rid)
        delete = self.app.request("DELETE", f"Observation/{rid}")
        expect(delete.status == 403, f"App role DELETE -> {delete.status}, expected 403")
        hist = self.app.request("GET", f"Observation/{rid}/_history")
        expect(hist.status == 403, f"App role history -> {hist.status}, expected 403")
        bundle = self.app.request("POST", "", json_body={"resourceType": "Bundle", "type": "batch", "entry": []})
        expect(bundle.status == 403, f"App role ProcessBundle -> {bundle.status}, expected 403")
        put = self.app.put(self.obs(rid, valueString="app-update"), if_match=f'W/"{current["meta"]["versionId"]}"')
        expect(put.ok, f"App role PUT of the same resource -> {put.status}")
        return {"unsigned": unsigned.status_code, "app read": ok.status, "app delete": delete.status, "app history": hist.status,
                "app bundle": bundle.status, "app put": put.status}

    # ---- V19 : cleanup and proof that the frozen data is untouched ----------------------------------------------
    def v19(self):
        # sweep by tag as well, so nothing tagged temporary survives even if a check crashed before tracking it
        for rtype in ("Observation", "Patient"):
            for e in self.search(rtype, [("_tag", TEMP_TAG_TOKEN)]):
                self.created.add((rtype, e["resource"]["id"]))
        for rtype, rid in sorted(self.created, key=lambda x: (x[0] != "Observation", x[1])):
            r = self.v.request("DELETE", f"{rtype}/{rid}")
            expect(r.status in (200, 204, 404, 410), f"DELETE {rtype} -> {r.status}")
        for rtype in ("Observation", "Patient"):  # search is eventually consistent: poll until the deletions are visible
            self.eventually(lambda t=rtype: len(self.search(t, [("_tag", TEMP_TAG_TOKEN)])) == 0)

        diffs: dict[str, list[str]] = {"versionId": [], "content": [], "history": []}
        checked = 0
        for (rtype, rid), frozen in self.frozen.items():
            got = self.v.read(rtype, rid)
            checked += 1
            if got["meta"].get("versionId") != "1":
                diffs["versionId"].append(f"{rtype}/{rid} versionId {got['meta'].get('versionId')}")
            if no_meta(got) != no_meta(frozen):
                diffs["content"].append(f"{rtype}/{rid}")
        lag = 0.0
        try:  # history is eventually consistent: wait until every frozen resource shows exactly one version
            lag = self.eventually(lambda: all(self.history_versions(t, i) == 1 for (t, i) in self.frozen), seconds=self.history_poll_seconds)
        except Fail:
            pass
        for (rtype, rid) in self.frozen:
            n = self.history_versions(rtype, rid)
            if n != 1:
                diffs["history"].append(f"{rtype}/{rid} history length {n}")
        summary = {k: len(v) for k, v in diffs.items()}
        expect(checked == 53 and not any(diffs.values()), f"{checked} checked; differences {summary}: {[x for v in diffs.values() for x in v][:8]}")
        totals: dict[str, int] = {}

        def counts_back_to_import() -> bool:
            totals.update({t: self.total(t) for t in EXPECTED_COUNTS})
            return totals == EXPECTED_COUNTS

        try:  # accurate totals also lag right after deletes (observed live: Patient 11 for a moment)
            self.eventually(counts_back_to_import)
        except Fail:
            pass
        expect(totals == EXPECTED_COUNTS, f"final counts {totals}")
        return {"deleted_temp_resources": len(self.created), "frozen_resources_checked": checked, "final_counts": totals,
                "all_versionId_1": True, "all_content_equal_to_frozen": True, "all_history_length_1": True, "history_lag_seconds": round(lag, 1)}

    # ---- driver -------------------------------------------------------------------------------------------------
    def run_all(self, checks: str | None = None) -> dict:
        started = datetime.now(timezone.utc).isoformat(timespec="seconds")
        plan = [("V0", "capability statement", self.v0), ("V1", "read patient-p001; Patient/P001 is 404", self.v1),
                ("V2", "identifier search", self.v2), ("V3", "counts per type (accurate totals)", self.v3),
                ("V4", "patient-scoped searches", self.v4), ("V5", "Binary note equals frozen note", self.v5),
                ("V6", "_include", self.v6), ("V7", "_revinclude", self.v7), ("V8", "_elements / SUBSETTED", self.v8),
                ("V9", "paging and _sort", self.v9), ("V10", "chaining and _has", self.v10),
                ("V11", "POST create (temp)", self.v11), ("V12", "PUT create + If-None-Match", self.v12),
                ("V13", "update, If-Match, history, vread", self.v13), ("V14", "POST idempotency key", self.v14),
                ("V15", "search consistency after write", self.v15), ("V16", "batch and transaction bundles", self.v16),
                ("V17", "strict validation rejects invalid resource", self.v17), ("V18", "authorization negatives", self.v18)]
        if checks:
            wanted = {c.strip().upper() for c in checks.split(",") if c.strip()}
            known = {vid for vid, _, _ in plan} | {"V19"}
            unknown = wanted - known
            if unknown:
                raise ValueError(f"unknown check id(s): {sorted(unknown)} (known: {sorted(known)})")
            plan = [(vid, name, fn) for vid, name, fn in plan if vid in wanted]
        for vid, name, fn in plan:
            self.run(vid, name, fn)
        self.run("V19", "cleanup and frozen data unchanged", self.v19)  # always runs, even after failures or a subset
        failed = [r["id"] for r in self.results if r["status"] != "PASS"]
        return {"startedAt": started, "finishedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "datastoreBase": self.v.base_url, "checks": checks or "all", "passed": len(self.results) - len(failed),
                "failed": failed, "requestCount": len(self.rids), "results": self.results}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--datastore-id", required=True)
    ap.add_argument("--verifier-role-arn", required=True)
    ap.add_argument("--app-role-arn", required=True)
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--out", default="phase3_verify_report.json")
    ap.add_argument("--checks", default=None,
                     help="comma-separated subset, e.g. V0,V1,V2,V3,V4,V5 for a shortened post-recreation check; default: all")
    args = ap.parse_args()
    verifier = HealthLakeClient(args.datastore_id, region=args.region, role_arn=args.verifier_role_arn, session_name="medsafety-verify")
    app = HealthLakeClient(args.datastore_id, region=args.region, role_arn=args.app_role_arn, session_name="medsafety-app")
    suite = Suite(verifier, app, lambda url: httpx.get(url, timeout=30))
    report = suite.run_all(args.checks)
    Path(args.out).write_text(json.dumps(report, indent=2))
    print(f"\n{report['passed']}/{len(report['results'])} checks passed; {report['requestCount']} requests; report: {args.out}")
    return 0 if not report["failed"] else 1


if __name__ == "__main__":
    sys.exit(main())
