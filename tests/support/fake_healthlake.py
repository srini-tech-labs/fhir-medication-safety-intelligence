"""In-memory fake of the AWS HealthLake FHIR REST API, for offline tests of the client, repository and verifier.

Faithful where it matters for correctness of OUR code:
  * verifies the SigV4 signature (recomputed with botocore against a known secret) -- unsigned/forged requests get 403;
  * per-credential IAM emulation (App vs Verifier action sets) -> 403 for actions the caller lacks;
  * versioning (meta.versionId, ETag W/"n"), If-Match / If-None-Match: * (412), PUT-creates-if-new, POST server ids,
    x-amz-fhir-idempotency-key (409 + original), soft delete (410) with history, instance/type history and vread;
  * search: identifier/_id/patient/subject/status/code/_tag, chaining, _has, _include / _revinclude, _elements (SUBSETTED),
    _summary, _total, _sort=_id, _count (max 100) and paging through Bundle.link[next]; unsupported params -> 400;
  * batch and transaction bundles (urn:uuid reference resolution, atomic rollback), strict validation of required elements.
It is NOT HealthLake: the live verification (V0-V19) is the authority.
"""
from __future__ import annotations

import copy
import json
import re
import urllib.parse
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials

REPO = Path(__file__).resolve().parents[2]
BULK = REPO / "data" / "phase0_v1_0" / "fhir" / "bulk"
DATASTORE_ID = "0123456789abcdef0123456789abcdef"
BASE = f"https://healthlake.us-east-1.amazonaws.com/datastore/{DATASTORE_ID}/r4/"
APP_KEY, VERIFIER_KEY = "AKIAAPPROLETEST00001", "AKIAVERIFIERTEST0001"
SECRETS = {APP_KEY: "app-secret-test-value", VERIFIER_KEY: "verifier-secret-test-value"}
APP_ACTIONS = {"ReadResource", "SearchWithGet", "SearchWithPost", "GetCapabilities", "CreateResource", "UpdateResource"}
VERIFIER_ACTIONS = APP_ACTIONS | {"GetHistoryByResourceId", "VersionReadResource", "ProcessBundle", "DeleteResource"}
PERMISSIONS = {APP_KEY: APP_ACTIONS, VERIFIER_KEY: VERIFIER_ACTIONS}


def credentials(key: str):
    return lambda: Credentials(key, SECRETS[key], None)


REQUIRED = {
    "Observation": ("status", "code"), "MedicationRequest": ("status", "intent", "subject"), "Encounter": ("status", "class"),
    "DocumentReference": ("status", "content"), "DetectedIssue": ("status",), "RiskAssessment": ("status", "subject"),
    "Binary": ("contentType",), "Patient": (),
}
REF_PARAMS = {"MedicationRequest": ("patient", "subject"), "Observation": ("patient", "subject"), "Encounter": ("patient", "subject"),
              "DocumentReference": ("patient", "subject"), "DetectedIssue": ("patient",), "RiskAssessment": ("subject", "patient")}
SEARCH_PARAMS = {"_id", "_count", "_revinclude", "_include", "_elements", "_summary", "_total", "_sort", "_tag", "_getpages", "_offset",
                 "identifier", "patient", "subject", "status", "code", "intent", "name"}
MANDATORY = {"Observation": {"status", "code"}, "MedicationRequest": {"status", "intent", "subject", "medicationCodeableConcept"},
             "Encounter": {"status", "class"}, "DocumentReference": {"status", "content"}}


def _aws_canonical_url(url: str) -> str:
    """What AWS computes server-side: every reserved character in a query key/value percent-encoded (SigV4 canonical query)."""
    base, sep, query = url.partition("?")
    if not sep:
        return url
    pairs = []
    for pair in query.split("&"):
        key, _, value = pair.partition("=")
        pairs.append(f"{urllib.parse.quote(urllib.parse.unquote(key), safe='')}={urllib.parse.quote(urllib.parse.unquote(value), safe='')}")
    return base + "?" + "&".join(pairs)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class FakeHealthLake:
    def __init__(self, *, seed: bool = True, page_size: int = 5):
        self.store: dict[str, dict[str, list[dict]]] = {}  # type -> id -> versions ({"resource":..., "deleted":bool})
        self.requests: list[dict] = []
        self.idem: dict[tuple[str, str], tuple[str, str]] = {}
        self.pages: dict[str, tuple[list[dict], list[dict]]] = {}
        self.page_size = page_size
        self.unsupported_revinclude: set[str] = set()  # search-parameter spellings (e.g. {"patient"}) answered with HTTP 400
        self.total_stale_reads = 0  # eventual consistency: this many `_total=accurate` reads still count soft-deleted resources
        self.history_empty_reads = 0  # eventual consistency: this many history reads return no entries before the truth shows
        self.fail_next: list[int] = []  # statuses to return (once each) before serving normally, e.g. [503, 429]
        if seed:
            for path in sorted(BULK.glob("*.ndjson")):
                for line in path.read_text("utf-8").splitlines():
                    self._commit(json.loads(line))

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    # ---- storage ---------------------------------------------------------------------------------------
    def _commit(self, resource: dict, *, deleted: bool = False) -> dict:
        versions = self.store.setdefault(resource["resourceType"], {}).setdefault(resource["id"], [])
        resource = copy.deepcopy(resource)
        resource["meta"] = {**resource.get("meta", {}), "versionId": str(len(versions) + 1), "lastUpdated": now()}
        versions.append({"resource": resource, "deleted": deleted})
        return resource

    def current(self, rtype: str, rid: str) -> dict | None:
        versions = self.store.get(rtype, {}).get(rid)
        return versions[-1]["resource"] if versions and not versions[-1]["deleted"] else None

    def all_current(self, rtype: str) -> list[dict]:
        return sorted((v[-1]["resource"] for v in self.store.get(rtype, {}).values() if not v[-1]["deleted"]), key=lambda r: r["id"])

    # ---- responses -------------------------------------------------------------------------------------
    @staticmethod
    def _resp(status: int, body=None, headers=None) -> httpx.Response:
        h = {"x-amzn-requestid": str(uuid.uuid4()), "content-type": "application/fhir+json", **(headers or {})}
        return httpx.Response(status, json=body, headers=h) if body is not None else httpx.Response(status, headers=h)

    def _outcome(self, status: int, diag: str, code: str = "processing") -> httpx.Response:
        return self._resp(status, {"resourceType": "OperationOutcome", "issue": [{"severity": "error", "code": code, "diagnostics": diag}]})

    # ---- auth ------------------------------------------------------------------------------------------
    def _authenticate(self, request: httpx.Request) -> str | None:
        m = re.match(r"AWS4-HMAC-SHA256 Credential=([^/]+)/(\d{8})/([^/]+)/([^/]+)/aws4_request, SignedHeaders=([^,]+), "
                     r"Signature=([0-9a-f]{64})$", request.headers.get("authorization", ""))
        if not m:
            return None
        key, _date, region, service, signed_headers, signature = m.groups()
        if service != "healthlake" or region != "us-east-1" or key not in SECRETS:
            return None
        aws = AWSRequest(method=request.method, url=_aws_canonical_url(str(request.url)), data=request.content,
                         headers={h: request.headers[h] for h in signed_headers.split(";")})
        aws.context["timestamp"] = request.headers["x-amz-date"]
        signer = SigV4Auth(Credentials(key, SECRETS[key], request.headers.get("x-amz-security-token")), "healthlake", region)
        expected = signer.signature(signer.string_to_sign(aws, signer.canonical_request(aws)), aws)
        return key if expected == signature else None

    # ---- entry point -----------------------------------------------------------------------------------
    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.fail_next:
            status = self.fail_next.pop(0)
            return self._resp(status, {"message": "injected failure"}, {"retry-after": "0"} if status == 429 else None)
        key = self._authenticate(request)
        url = request.url
        prefix = f"/datastore/{DATASTORE_ID}/r4"
        if not url.path.startswith(prefix):
            return self._resp(404, {"message": "unknown datastore"})
        segs = [urllib.parse.unquote(s) for s in url.path[len(prefix):].split("/") if s]
        action = self._action(request.method, segs)
        self.requests.append({"method": request.method, "path": "/".join(segs), "action": action, "key": key,
                              "headers": dict(request.headers), "query": url.query.decode()})
        if key is None:
            return self._resp(403, {"message": "The security token included in the request is invalid."})
        if action not in PERMISSIONS[key]:
            return self._resp(403, {"message": f"User: arn:aws:sts::123456789012:assumed-role/test/{key} is not authorized to perform: "
                                                f"healthlake:{action} on resource: {BASE}"})
        params = urllib.parse.parse_qsl(url.query.decode(), keep_blank_values=True)
        try:
            return self._route(request, segs, params)
        except _Reject as exc:
            return self._outcome(exc.status, str(exc))

    @staticmethod
    def _action(method: str, segs: list[str]) -> str:
        if segs == ["metadata"]:
            return "GetCapabilities"
        if not segs:
            return "ProcessBundle"
        if "_history" in segs:
            return "VersionReadResource" if segs[-2:-1] == ["_history"] and len(segs) >= 4 else "GetHistoryByResourceId"
        return {"GET": "ReadResource" if len(segs) > 1 else "SearchWithGet", "POST": "CreateResource", "PUT": "UpdateResource",
                "DELETE": "DeleteResource"}[method]

    # ---- routing ---------------------------------------------------------------------------------------
    def _route(self, req: httpx.Request, segs: list[str], params) -> httpx.Response:
        m = req.method
        if segs == ["metadata"]:
            return self._resp(200, {"resourceType": "CapabilityStatement", "fhirVersion": "4.0.1", "status": "active"})
        if not segs:
            return self._bundle_request(req)
        rtype = segs[0]
        if len(segs) == 1:
            if m == "GET":
                return self._search(rtype, params)
            if m == "POST":
                return self._create(req, rtype)
        if len(segs) >= 2 and segs[1] != "_history":
            rid = segs[1]
            if len(segs) == 2:
                if m == "GET":
                    return self._read(rtype, rid)
                if m == "PUT":
                    return self._put(req, rtype, rid)
                if m == "DELETE":
                    return self._delete(rtype, rid)
            if segs[2:3] == ["_history"]:
                versions = self.store.get(rtype, {}).get(rid)
                if not versions:
                    return self._outcome(404, f"{rtype}/{rid} not found", "not-found")
                if len(segs) == 3:
                    if self.history_empty_reads > 0:
                        self.history_empty_reads -= 1
                        return self._resp(200, {"resourceType": "Bundle", "type": "history", "total": 0})
                    entries = [{"resource": v["resource"], "response": {"status": "200"}} for v in reversed(versions)]
                    return self._resp(200, {"resourceType": "Bundle", "type": "history", "total": len(entries), "entry": entries})
                for v in versions:
                    if v["resource"]["meta"]["versionId"] == segs[3]:
                        return self._resp(200, v["resource"], {"etag": f'W/"{segs[3]}"'})
                return self._outcome(404, "version not found", "not-found")
        if len(segs) == 2 and segs[1] == "_history":
            entries = [{"resource": v["resource"]} for vs in self.store.get(rtype, {}).values() for v in reversed(vs)]
            return self._resp(200, {"resourceType": "Bundle", "type": "history", "total": len(entries), "entry": entries})
        return self._outcome(400, "unsupported interaction", "not-supported")

    # ---- read / write ---------------------------------------------------------------------------------
    def _read(self, rtype: str, rid: str) -> httpx.Response:
        versions = self.store.get(rtype, {}).get(rid)
        if not versions:
            return self._outcome(404, f"{rtype}/{rid} not found", "not-found")
        if versions[-1]["deleted"]:
            return self._outcome(410, f"{rtype}/{rid} was deleted", "deleted")
        res = versions[-1]["resource"]
        return self._resp(200, res, {"etag": f'W/"{res["meta"]["versionId"]}"'})

    def _validate(self, req: httpx.Request, rtype: str, body: dict, *, rid: str | None = None) -> None:
        if req.headers.get("x-amzn-healthlake-fhir-validation-level", "strict") == "minimal":
            return
        if not isinstance(body, dict) or body.get("resourceType") != rtype:
            raise _Reject(400, f"resourceType must be {rtype}")
        if rid is not None and body.get("id") != rid:
            raise _Reject(400, "resource id does not match the URL")
        missing = [f for f in REQUIRED.get(rtype, ()) if f not in body]
        if missing:
            raise _Reject(400, f"FHIR resource in payload failed FHIR validation rules: missing {', '.join(missing)}")

    def _create(self, req: httpx.Request, rtype: str) -> httpx.Response:
        body = json.loads(req.content or b"{}")
        self._validate(req, rtype, body)
        idem = req.headers.get("x-amz-fhir-idempotency-key")
        key = self.requests[-1]["key"]
        if idem:
            if not re.fullmatch(r"[0-9a-fA-F-]{36}", idem):
                return self._outcome(400, "malformed idempotency key")
            if (key, idem) in self.idem:
                t, i = self.idem[(key, idem)]
                return self._resp(409, {"resourceType": "OperationOutcome", "issue": [{"severity": "error", "code": "duplicate",
                                  "diagnostics": "A resource is already created using the provided idempotency Key"}]}, {"location": f"{t}/{i}"})
        body = {**body, "id": str(uuid.uuid4())}
        stored = self._commit(body)
        if idem:
            self.idem[(key, idem)] = (rtype, stored["id"])
        return self._resp(201, stored, {"etag": 'W/"1"'})  # the real service sends no Location header on POST

    def _put(self, req: httpx.Request, rtype: str, rid: str) -> httpx.Response:
        body = json.loads(req.content or b"{}")
        self._validate(req, rtype, body, rid=rid)
        existing = self.current(rtype, rid)
        if req.headers.get("if-none-match") == "*" and (existing or self.store.get(rtype, {}).get(rid)):  # deleted ids stay taken (412)
            return self._outcome(412, "resource already exists", "conflict")
        if "if-match" in req.headers and (not existing or req.headers["if-match"] != f'W/"{existing["meta"]["versionId"]}"'):
            return self._outcome(412, "ETag does not match the current version", "conflict")
        stored = self._commit(body)
        return self._resp(200 if existing else 201, stored, {"etag": f'W/"{stored["meta"]["versionId"]}"'})

    def _delete(self, rtype: str, rid: str) -> httpx.Response:
        existing = self.current(rtype, rid)
        if not existing:
            return self._outcome(404, "not found", "not-found")
        self._commit(existing, deleted=True)
        return self._resp(204)

    # ---- search ---------------------------------------------------------------------------------------
    def _ref_matches(self, res: dict, params: tuple[str, ...], value: str) -> bool:
        wanted = {value, f"Patient/{value}"} if "/" not in value else {value}
        # R4 defines `patient` and `subject` as equivalent search parameters over Resource.subject / Resource.patient
        return any((res.get(f) or {}).get("reference") in wanted for p in params if p in ("patient", "subject")
                   for f in ("subject", "patient"))

    def _token(self, values: list[dict], token: str) -> bool:
        system, _, code = token.rpartition("|") if "|" in token else ("", "", token)
        return any((not system or v.get("system") == system) and (not code or v.get("value", v.get("code")) == code) for v in values)

    def _matches(self, rtype: str, res: dict, params: list[tuple[str, str]]) -> bool:
        for name, value in params:
            base = name.split(":")[0]
            if name.startswith("_has:"):
                _, h_type, h_param, h_filter = name.split(":", 3) if name.count(":") >= 3 else (None, None, None, None)
                fname = h_filter
                if not any(self._ref_matches(r, (h_param,), f"{rtype}/{res['id']}") and r.get(fname) == value
                           for r in self.all_current(h_type)):
                    return False
            elif "." in name:  # chaining e.g. subject:Patient.identifier
                ref, _, chain = name.partition(":")[2].partition(".") if ":" in name else ("", "", "")
                target = self.current(ref, ((res.get(name.split(":")[0]) or {}).get("reference", "/").split("/")[-1]))
                if not target or not self._token(target.get(chain, []), value):
                    return False
            elif base == "identifier":
                if not self._token(res.get("identifier", []), value):
                    return False
            elif base == "_id":
                if res["id"] not in value.split(","):
                    return False
            elif base in ("patient", "subject"):
                if not self._ref_matches(res, (base,), value):
                    return False
            elif base == "status":
                if res.get("status") != value:
                    return False
            elif base == "code":
                if not self._token((res.get("code") or {}).get("coding", []), value.replace("|", "|")):
                    return False
            elif base == "_tag":
                if not self._token((res.get("meta") or {}).get("tag", []), value):
                    return False
        return True

    def _search(self, rtype: str, params: list[tuple[str, str]]) -> httpx.Response:
        if rtype not in REQUIRED and rtype not in ("Patient",):
            return self._outcome(400, f"unsupported resource type {rtype}", "not-supported")
        q = dict(params)
        if "_getpages" in q:
            matches, includes = self.pages[q["_getpages"]]
            offset, count = int(q.get("_offset", 0)), int(q.get("_count", self.page_size))
            return self._page(rtype, matches, includes, offset, count, q["_getpages"], {})
        for name, _ in params:
            base = name.split(":")[0].split(".")[0]
            if base not in SEARCH_PARAMS and not name.startswith("_has:") and "." not in name:
                return self._outcome(400, f"search parameter {name} is not supported for {rtype}", "not-supported")
        if "_summary" in q and "_elements" in q:
            return self._outcome(400, "_summary cannot be combined with _elements")
        count = int(q.get("_count", self.page_size))
        if count > 100 or count < 1:
            return self._outcome(400, "_count must be between 1 and 100")
        filters = [(n, v) for n, v in params if not n.startswith("_") or n.startswith("_has:") or n in ("_id", "_tag")]
        matches = [r for r in self.all_current(rtype) if self._matches(rtype, r, filters)]
        if q.get("_sort", "_id") not in ("_id", "-_id"):
            return self._outcome(400, "unsupported _sort")
        matches.sort(key=lambda r: r["id"], reverse=q.get("_sort") == "-_id")
        includes = self._includes(matches, [v for n, v in params if n == "_include"], [v for n, v in params if n == "_revinclude"])
        if isinstance(includes, httpx.Response):
            return includes
        token = uuid.uuid4().hex + "=="  # real page tokens end in `==`: they must be percent-encoded before signing
        self.pages[token] = (matches, includes)
        extra = {}
        if q.get("_total") == "accurate":
            extra["total"] = len(matches)
            if self.total_stale_reads > 0 and rtype == "Patient" and not filters and any(v[-1]["deleted"] for v in self.store.get("Patient", {}).values()):
                self.total_stale_reads -= 1
                extra["total"] += 1  # a just-deleted resource is still counted for a moment
        return self._page(rtype, matches, includes, 0, count, token, {"_elements": q.get("_elements"), "_summary": q.get("_summary"), **extra})

    def _includes(self, matches, inc, rev):
        out: list[dict] = []
        for spec in inc:
            t, _, p = spec.partition(":")
            for m in matches:
                ref = (m.get(p) or {}).get("reference")
                if ref:
                    target = self.current(*ref.split("/", 1))
                    if target and target not in out:
                        out.append(target)
        for spec in rev:
            t, _, p = spec.partition(":")
            if t not in REF_PARAMS or p not in REF_PARAMS[t] or p in self.unsupported_revinclude:
                return self._outcome(400, f"_revinclude {spec} is not a supported search parameter")
            for m in matches:
                out += [r for r in self.all_current(t) if self._ref_matches(r, (p,), f"{m['resourceType']}/{m['id']}") and r not in out]
        return out

    def _page(self, rtype, matches, includes, offset, count, token, opts) -> httpx.Response:
        chunk = matches[offset:offset + count]
        elements = opts.get("_elements")

        def trim(r):
            if not elements:
                return r
            keep = {"resourceType", "id", "meta", *elements.split(","), *MANDATORY.get(r["resourceType"], set())}
            out = {k: v for k, v in r.items() if k in keep}
            out["meta"] = {**out.get("meta", {}), "tag": [*out.get("meta", {}).get("tag", []), {"code": "SUBSETTED"}]}
            return out

        entries = [{"fullUrl": f"{BASE}{r['resourceType']}/{r['id']}", "resource": trim(r), "search": {"mode": "match"}} for r in chunk]
        if includes and offset == 0:  # includes ride on the first page in this fake
            entries += [{"fullUrl": f"{BASE}{r['resourceType']}/{r['id']}", "resource": r, "search": {"mode": "include"}} for r in includes]
        bundle: dict = {"resourceType": "Bundle", "type": "searchset", "entry": entries, "link": []}
        if "total" in opts:
            bundle["total"] = opts["total"]
        if offset + count < len(matches):
            bundle["link"].append({"relation": "next", "url": f"{BASE}{rtype}?_getpages={token}&_offset={offset + count}&_count={count}"})
        return self._resp(200, bundle)

    # ---- bundles ---------------------------------------------------------------------------------------
    def _bundle_request(self, req: httpx.Request) -> httpx.Response:
        bundle = json.loads(req.content or b"{}")
        if bundle.get("resourceType") != "Bundle" or bundle.get("type") not in ("batch", "transaction"):
            return self._outcome(400, "Bundle type must be batch or transaction")
        entries, atomic = bundle.get("entry", []), bundle["type"] == "transaction"
        if len(entries) > 100:
            return self._outcome(422, "synchronous transactions are limited to 100 entries")
        uuids = {e["fullUrl"]: str(uuid.uuid4()) for e in entries if e.get("fullUrl", "").startswith("urn:uuid:")}

        def resolve(node):  # urn:uuid references -> server-assigned Type/id
            if isinstance(node, dict):
                out = {}
                for k, v in node.items():
                    if k == "reference" and v in uuids:
                        target = next(e for e in entries if e.get("fullUrl") == v)["resource"]["resourceType"]
                        out[k] = f"{target}/{uuids[v]}"
                    else:
                        out[k] = resolve(v)
                return out
            return [resolve(i) for i in node] if isinstance(node, list) else node

        results, staged = [], []
        for e in entries:
            method, url = e["request"]["method"], e["request"]["url"]
            rtype = url.split("/")[0].split("?")[0]
            try:
                if method == "POST":
                    res = resolve(e["resource"])
                    self._validate(req, rtype, res)
                    res = {**res, "id": uuids.get(e.get("fullUrl"), str(uuid.uuid4()))}
                    staged.append(("put", res))
                    results.append({"response": {"status": "201 Created", "location": f"{rtype}/{res['id']}/_history/1", "etag": 'W/"1"'}})
                elif method == "PUT":
                    rid = url.split("/")[1]
                    self._validate(req, rtype, e["resource"], rid=rid)
                    staged.append(("put", e["resource"]))
                    results.append({"response": {"status": "200 OK"}})
                elif method == "DELETE":
                    staged.append(("del", (rtype, url.split("/")[1])))
                    results.append({"response": {"status": "204 No Content"}})
                else:
                    raise _Reject(400, f"unsupported bundle method {method}")
            except _Reject as exc:
                if atomic:
                    return self._outcome(400, f"transaction failed, nothing committed: {exc}")
                results.append({"response": {"status": f"{exc.status}", "outcome": {"resourceType": "OperationOutcome",
                                "issue": [{"severity": "error", "code": "processing", "diagnostics": str(exc)}]}}})
                staged.append(None)
        for op in staged:
            if op is None:
                continue
            if op[0] == "put":
                self._commit(op[1])
            else:
                cur = self.current(*op[1])
                if cur:
                    self._commit(cur, deleted=True)
        return self._resp(200, {"resourceType": "Bundle", "type": f"{bundle['type']}-response", "entry": results})


class _Reject(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
