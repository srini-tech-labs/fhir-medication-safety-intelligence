#!/usr/bin/env python
"""Phase 4 verification of the deployed API (A0-A16): read -> analyze -> explain against live HealthLake.

    # 1. direct Lambda invocation (no API Gateway yet): read + analyze golden parity
    backend/.venv/bin/python scripts/api_verify.py --mode lambda --function medsafety-api --checks A0,A2,A3,A4,A5,A6,A9,A10,A14,A15 ...
    # 2. over HTTPS through API Gateway from the allow-listed IP (all checks)
    backend/.venv/bin/python scripts/api_verify.py --mode http --base-url https://<id>.execute-api.us-east-1.amazonaws.com/dev ...

* Local truth: the same deterministic engine over the frozen local files (`LocalFHIRRepository`) plus the golden expectations
  (`expected_results.json` with the approved P006 override).
* Every check reports PASS / FAIL / SKIP (a check needing an adapter that was not provided says SKIP, never PASS).
* Nothing here writes to HealthLake. A14 proves nothing was written; A15 proves the deployed role cannot write.
* The report holds ids, statuses, counts and latencies -- never clinical text or model output.
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "backend"))

from app.config import Settings  # noqa: E402
from app.container import build_container  # noqa: E402
from app.services.explanation import failures  # noqa: E402
from app.services.explanation.mock import MockExplanationService  # noqa: E402

PACKAGE = REPO / "data" / "phase0_v1_0"
PATIENTS = [f"P{n:03d}" for n in range(1, 11)]
ORIGIN = "http://localhost:5173"
EXPECTED_MODEL = "us.amazon.nova-2-lite-v1:0"
PUBLIC_FALLBACK_CODES = {failures.UNAVAILABLE, failures.TEMPORARY, failures.INCOMPLETE, failures.DECLINED, failures.REJECTED}
ACCEPTANCE_BASELINE, ACCEPTANCE_FLOOR = 0.90, 0.80  # Phase 2 Sonnet 5 pooled acceptance 18/20; below the floor the phase stops
VOLATILE = ("generatedAt", "analysisId")
HEALTHLAKE_READ_ACTIONS = {"healthlake:ReadResource", "healthlake:SearchWithGet", "healthlake:GetCapabilities"}
ACCESS_LINE_FIELDS = {"method", "route", "status", "ms", "requestId"}  # nothing else may be logged per request
SECRET_LIKE = re.compile(r"KEY|SECRET|(?<!MAX_)TOKEN|PASSWORD")  # BEDROCK_MAX_TOKENS is a limit, not a credential
# docs/adr/0011: OPENAI_API_KEY_SECRET_ARN is a Secrets Manager ARN *pointer*, not a credential value -- the name
# itself legitimately contains "KEY"/"SECRET" and would otherwise false-positive here. Its value is checked
# separately (must look like an ARN) so a real key accidentally placed under that name is still caught.
SECRET_LIKE_ALLOWED_NAMES = {"OPENAI_API_KEY_SECRET_ARN"}
BEDROCK_ALLOWED_ACTIONS = {"bedrock:InvokeModel", "bedrock:GetInferenceProfile"}  # Gate B statements only (no streaming, no wildcard)
EXPECTED_COUNTS = {"Patient": 10, "Encounter": 10, "MedicationRequest": 14, "Observation": 11, "DocumentReference": 4, "Binary": 4}


@dataclass
class Reply:
    status: int
    body: object
    headers: dict
    ms: float


class Fail(AssertionError):
    pass


class Skip(Exception):
    """A check that has nothing to inspect right now (reported SKIP with the reason -- never PASS)."""


def expect(cond, message: str):
    if not cond:
        raise Fail(message)


# ---- transports ----------------------------------------------------------------------------------------------
def rest_event(method: str, path: str, body=None, headers=None) -> dict:
    hdrs = {"Host": "abc123.execute-api.us-east-1.amazonaws.com", "Content-Type": "application/json", **(headers or {})}
    return {"resource": path, "path": path, "httpMethod": method, "headers": hdrs, "multiValueHeaders": {k: [v] for k, v in hdrs.items()},
            "queryStringParameters": None, "multiValueQueryStringParameters": None, "pathParameters": None, "stageVariables": None,
            "requestContext": {"requestId": "verify", "stage": "dev", "path": f"/dev{path}", "httpMethod": method, "resourcePath": path,
                               "accountId": "000000000000", "apiId": "abc123", "identity": {"sourceIp": "127.0.0.1", "userAgent": "api_verify"}},
            "body": json.dumps(body) if body is not None else None, "isBase64Encoded": False}


def parse_body(text):
    try:
        return json.loads(text) if text else None
    except (json.JSONDecodeError, TypeError):
        return text


class HttpTransport:
    name = "http"

    def __init__(self, base_url: str, timeout: float = 40.0):
        import httpx

        self.base, self.client = base_url.rstrip("/"), httpx.Client(timeout=timeout)

    def call(self, method, path, body=None, headers=None) -> Reply:
        started = time.perf_counter()
        r = self.client.request(method, self.base + path, json=body, headers=headers)
        return Reply(r.status_code, parse_body(r.text), dict(r.headers), (time.perf_counter() - started) * 1000)


class LambdaTransport:
    name = "lambda"

    def __init__(self, function: str, region: str = "us-east-1"):
        import boto3
        from botocore.config import Config

        self.function = function
        self.client = boto3.client("lambda", region_name=region, config=Config(read_timeout=60, retries={"total_max_attempts": 1}))

    def call(self, method, path, body=None, headers=None) -> Reply:
        started = time.perf_counter()
        out = self.client.invoke(FunctionName=self.function, Payload=json.dumps(rest_event(method, path, body, headers)).encode())
        payload = json.loads(out["Payload"].read())
        if out.get("FunctionError"):
            return Reply(502, {"functionError": out["FunctionError"]}, {}, (time.perf_counter() - started) * 1000)
        return Reply(payload["statusCode"], parse_body(payload.get("body")), payload.get("headers") or {}, (time.perf_counter() - started) * 1000)


class InProcessTransport:
    """The Lambda handler called in-process (unit tests, and a local rehearsal of the suite)."""
    name = "inprocess"

    def __init__(self, handler):
        self.handler = handler

    def call(self, method, path, body=None, headers=None) -> Reply:
        started = time.perf_counter()
        r = self.handler(rest_event(method, path, body, headers), None)
        return Reply(r["statusCode"], parse_body(r.get("body")), r.get("headers") or {}, (time.perf_counter() - started) * 1000)


# ---- the suite ---------------------------------------------------------------------------------------------------
def expected_for(pid: str, expected: list, overrides: dict) -> dict:
    base = next(p for p in expected if p["patientId"] == pid)
    return {**base, **{k: v for k, v in overrides.get(pid, {}).items() if k != "reason"}}


def scrub(analysis: dict) -> dict:
    """Drop the fields that legitimately differ between runs/backends (time, the numbered ids)."""
    out = {k: v for k, v in analysis.items() if k not in VOLATILE}
    if isinstance(out.get("riskAssessment"), dict):
        out["riskAssessment"] = {k: v for k, v in out["riskAssessment"].items() if k != "id"}
    return out


class Suite:
    def __init__(self, transport, *, explain_runs: int = 3, dynamodb=None, table: str | None = None, logs=None, log_group: str | None = None,
                 iam=None, role_name: str | None = None, lambda_client=None, function: str | None = None, healthlake=None,
                 sleep: Callable[[float], None] = time.sleep, expected_model: str = EXPECTED_MODEL, burst: int = 40, sustained: int = 160,
                 pending: dict[str, str] | None = None, check_fallback: bool = False):
        self.t, self.explain_runs, self.sleep = transport, explain_runs, sleep
        self.dynamodb, self.table, self.logs, self.log_group = dynamodb, table, logs, log_group
        self.iam, self.role_name, self.lambda_client, self.function, self.healthlake = iam, role_name, lambda_client, function, healthlake
        self.expected_model, self.burst, self.sustained = expected_model, burst, sustained
        self.check_fallback = check_fallback
        self.reached_lambda = 0      # requests API Gateway forwarded (excludes gateway-only 403/429 answers)
        self.gateway_rejected = 0    # answered by API Gateway itself: unknown route (403 Missing Authentication Token) or throttled (429)
        self.throttled_retries = 0   # 429s that were retried (never in A11)
        self._last_call = 0.0
        self.min_interval = 0.22 if getattr(transport, 'name', '') == 'http' else 0.0  # stay under the 5 req/s stage limit
        self.pending = pending or {}  # check id -> why it cannot run yet (e.g. depends on an external service that is not enabled)
        self.started_ms = int(time.time() * 1000)
        self.results: list[dict] = []
        self.latencies: list[tuple[str, float]] = []
        self.analyses: dict[str, str] = {}
        self.explain_stats: dict = {}
        self.probe_text: list[str] = []
        tmp = tempfile.mkdtemp(prefix="api-verify-local-")
        base = Settings.from_env()
        cfg = Settings(**{**base.__dict__, "data_backend": "local", "package_dir": PACKAGE, "output_dir": Path(tmp), "explanation_mode": "mock",
                          "healthlake_write_outputs": False})
        self.local = build_container(cfg, explainer=MockExplanationService())
        self.expected = json.loads((PACKAGE / "expected" / "expected_results.json").read_text("utf-8"))["patients"]
        self.overrides = json.loads((REPO / "tests" / "golden" / "expected_overrides.json").read_text("utf-8"))["patients"]

    # -- plumbing
    @staticmethod
    def gateway_only(r: Reply) -> bool:
        """API Gateway answered by itself (the Lambda never ran): throttled, or a route that is not part of the contract."""
        api_gateway_json = isinstance(r.body, dict) and set(r.body) == {"message"}
        return r.status == 429 or (r.status == 403 and api_gateway_json) or (r.status in (500, 502, 503, 504) and api_gateway_json)

    def call(self, method, path, *, retry_throttle: bool = True, **kw) -> Reply:
        for attempt in range(4):
            wait = self.min_interval - (time.monotonic() - self._last_call)
            if wait > 0:
                self.sleep(wait)
            self._last_call = time.monotonic()
            r = self.t.call(method, path, **kw)
            if self.gateway_only(r):
                self.gateway_rejected += 1
            else:
                self.reached_lambda += 1
            if r.status == 429 and retry_throttle and attempt < 3:  # the stage limit is doing its job; back off, do not fail the check
                self.throttled_retries += 1
                self.sleep(1.0)
                continue
            break
        self.latencies.append((f"{method} {re.sub(r'/P[0-9]+', '/{id}', path)}", r.ms))
        return r

    def ok(self, method, path, **kw):
        r = self.call(method, path, **kw)
        expect(r.status == 200, f"{method} {path} -> {r.status}")
        return r.body

    def run(self, cid: str, name: str, fn: Callable[[], dict | None], *, needs: object = True, why: str = "") -> None:
        if not needs:
            self.results.append({"id": cid, "name": name, "status": "SKIP", "detail": {"reason": why}})
            print(f"{cid:>4} SKIP {name}  ({why})", flush=True)
            return
        started = time.monotonic()
        try:
            detail, status = fn() or {}, "PASS"
        except Skip as exc:
            detail, status = {"reason": str(exc)}, "SKIP"
        except Fail as exc:
            detail, status = {"reason": str(exc)}, "FAIL"
        except Exception as exc:  # noqa: BLE001 - a crashing check is a failed check, never a skip
            detail, status = {"reason": f"{type(exc).__name__}: {str(exc)[:300]}"}, "FAIL"
        self.results.append({"id": cid, "name": name, "status": status, "seconds": round(time.monotonic() - started, 2), "detail": detail})
        print(f"{cid:>4} {status:<4} {name}" + ("" if status == "PASS" else f"  -> {detail.get('reason')}"), flush=True)

    # -- checks
    def a0(self):
        r = self.call("GET", "/v1/health")
        expect(r.status == 200 and r.body == {"status": "ok"}, f"health -> {r.status} {r.body}")

    def a1(self):
        for path in ("/docs", "/openapi.json", "/redoc", "/"):
            r = self.call("GET", path)
            expect(r.status in (403, 404), f"{path} is reachable through the API: HTTP {r.status}")
        return {"undocumented routes": "blocked"}

    def a2(self):
        body = self.ok("GET", "/v1/patients")
        expect([p["id"] for p in body["patients"]] == PATIENTS, "patient list differs from P001-P010")
        expect(body == json.loads(json.dumps({"patients": [p.model_dump(mode="json", by_alias=True) for p in self.local.snapshots.list_patients()]})),
               "patient list differs from the local backend")

    def a3(self):
        for pid in PATIENTS:
            got = self.ok("GET", f"/v1/patients/{pid}/snapshot")
            expect(got == self.local.snapshots.snapshot(pid).model_dump(mode="json", by_alias=True), f"{pid} snapshot differs from local")
        return {"snapshots_equal_local": len(PATIENTS)}

    def a4(self):
        checked = 0
        for pid in PATIENTS:
            for doc in self.local.snapshots.snapshot(pid).documents:
                got = self.ok("GET", f"/v1/patients/{pid}/documents/{doc.id}")
                frozen = self.local.snapshots.document(pid, doc.id).model_dump(mode="json", by_alias=True)
                expect(got == frozen, f"{pid}/{doc.id} differs from the frozen note")
                self.probe_text.append(got["text"][:40])
                checked += 1
        expect(checked == 4, f"{checked} notes checked, expected 4")
        return {"notes_byte_equal": checked}

    def f1(self):
        """The labelled fallback, as stored by an earlier explanation attempt that Bedrock could not serve, is what the API returns:
        public code and reason only, no provider detail, deterministic result untouched. Reads the stored analysis: NO model call."""
        latest = self.ok("GET", "/v1/patients/P001/analyses/latest")
        ex = latest.get("aiExplanation")
        if ex is None:  # the API only exposes the latest analysis; a newer analysis hides an older stored fallback -- nothing to inspect, not a pass
            raise Skip("the latest P001 analysis has no stored explanation, so there is no fallback to inspect (this check makes no model call)")
        expect(ex["mode"] == "mock" and ex.get("model") is None, f"expected a labelled fallback, found mode={ex['mode']}")
        code = ex.get("fallbackCode")
        expect(code in PUBLIC_FALLBACK_CODES, f"fallback code {code!r} is not a public category")
        expect(ex.get("fallbackReason") == failures.public_reason(code), "fallback reason is not the public text for its code")
        expect(ex.get("groundedInFindingsOnly") is True, "fallback is not marked grounded")
        text = json.dumps(ex).lower()
        for pattern in (r"\bbedrock\b", r"accessdenied", r"validationexception", r"\bconverse\b", r"\bnova\b", r"arn:aws", r"traceback",
                        r"account is currently", r"request ?id", r"\bboto"):
            expect(not re.search(pattern, text), f"the fallback exposes provider detail matching {pattern}")
        local = self.local.analyses.analyze("P001").model_dump(mode="json", by_alias=True)
        for key in ("overallSeverity", "findings", "dataGaps", "summary"):
            expect(latest[key] == local[key], f"the stored analysis' deterministic field {key} differs from the local engine")
        return {"served_from": latest["analysisId"], "fallbackCode": code, "public_reason_only": True, "deterministic_result_unchanged": True}

    def a5(self):
        for pid in PATIENTS:
            got = self.ok("POST", f"/v1/patients/{pid}/analyses")
            self.analyses[pid] = got["analysisId"]
            exp = expected_for(pid, self.expected, self.overrides)
            expect(got["patientId"] == pid and got["status"] == "COMPLETED", f"{pid} status")
            expect(got["overallSeverity"] == exp["expectedOverallSeverity"], f"{pid} overall {got['overallSeverity']} != {exp['expectedOverallSeverity']}")
            expect(sorted(f["ruleId"] for f in got["findings"]) == sorted(exp["expectedFindingRuleIds"]), f"{pid} findings")
            expect(sorted(g["ruleId"] for g in got["dataGaps"]) == sorted(exp["expectedDataGapRuleIds"]), f"{pid} data gaps")
            expect(got["aiExplanation"] is None, f"{pid} analyze must not include an AI explanation")
            local = self.local.analyses.analyze(pid).model_dump(mode="json", by_alias=True)
            expect(scrub(got) == scrub(local), f"{pid} analysis differs from the local backend")
        p8 = self.ok("POST", "/v1/patients/P008/analyses")
        expect([(f["ruleId"], f["severity"]) for f in p8["findings"]] == [("DL-001", "HIGH"), ("DDI-003", "MODERATE")], "P008 order")
        return {"golden_and_local_parity": len(PATIENTS)}

    def a6(self):
        for pid, first in self.analyses.items():
            n1 = int(first.rsplit("-", 1)[1])
            latest = self.ok("GET", f"/v1/patients/{pid}/analyses/latest")
            expect(int(latest["analysisId"].rsplit("-", 1)[1]) >= n1, f"{pid} latest is older than the analysis just run")
            again = self.ok("POST", f"/v1/patients/{pid}/analyses")
            expect(int(again["analysisId"].rsplit("-", 1)[1]) == int(latest["analysisId"].rsplit("-", 1)[1]) + 1, f"{pid} numbering did not increment")
            self.analyses[pid] = again["analysisId"]
        detail = {"persisted": len(self.analyses)}
        if self.dynamodb and self.table:
            table = self.dynamodb.describe_table(TableName=self.table)["Table"]
            expect("SSEDescription" not in table, "table does not use the default AWS-owned encryption")
            expect(table["BillingModeSummary"]["BillingMode"] == "PAY_PER_REQUEST", "table is not on-demand")
            detail["table"] = "default AWS-owned encryption, on-demand"
        return detail

    def a7(self):
        runs, stats = [], {"llm": 0, "fallback": 0, "codes": {}, "models": set(), "latency_ms": []}
        for pid in PATIENTS:
            for _ in range(self.explain_runs):
                analysis = self.ok("POST", f"/v1/patients/{pid}/analyses")
                r = self.call("POST", f"/v1/patients/{pid}/analyses/{analysis['analysisId']}/explanation")
                expect(r.status == 200, f"{pid} explain -> {r.status}")
                ex = r.body
                stats["latency_ms"].append(round(r.ms))
                if ex["mode"] == "llm":
                    stats["llm"] += 1
                    stats["models"].add(ex["model"])
                    expect(ex["model"] == self.expected_model, f"{pid} answered by {ex['model']}")
                    expect(ex.get("groundedInFindingsOnly") is True, f"{pid} llm output not marked grounded")
                    self.probe_text.append(ex["text"][:40])
                else:
                    stats["fallback"] += 1
                    code = ex.get("fallbackCode")
                    stats["codes"][code] = stats["codes"].get(code, 0) + 1
                    expect(code in PUBLIC_FALLBACK_CODES, f"{pid} fallback without a public code: {code}")
                    expect(ex.get("fallbackReason") == failures.public_reason(code), f"{pid} fallback reason is not the public text")
                runs.append(pid)
        total = len(runs)
        rate = stats["llm"] / total
        self.explain_stats = {"total": total, "llm": stats["llm"], "fallback": stats["fallback"], "acceptance": round(rate, 3),
                              "baseline_phase2_sonnet5": ACCEPTANCE_BASELINE, "codes": stats["codes"], "models": sorted(stats["models"]),
                              "latency_ms_p50": int(statistics.median(stats["latency_ms"])), "latency_ms_max": max(stats["latency_ms"])}
        expect(rate >= ACCEPTANCE_FLOOR, f"guard acceptance {stats['llm']}/{total} = {rate:.0%} is below the {ACCEPTANCE_FLOOR:.0%} floor (Phase 2 baseline "
                                         f"{ACCEPTANCE_BASELINE:.0%}); STOP and report. Fallback codes: {stats['codes']}")
        return self.explain_stats

    def a8(self):
        for pid in PATIENTS:
            analysis = self.ok("POST", f"/v1/patients/{pid}/analyses")
            first = self.call("POST", f"/v1/patients/{pid}/analyses/{analysis['analysisId']}/explanation")
            if first.status == 200 and first.body["mode"] == "llm":
                second = self.call("POST", f"/v1/patients/{pid}/analyses/{analysis['analysisId']}/explanation")
                expect(second.status == 200 and second.body == first.body, "a stored successful explanation was not returned unchanged")
                expect(second.ms < 3000, f"repeat explain took {second.ms:.0f} ms (expected a stored answer, no model call)")
                latest = self.ok("GET", f"/v1/patients/{pid}/analyses/latest")
                expect(latest["aiExplanation"]["text"] == first.body["text"], "explanation not persisted with the analysis")
                return {"idempotent_on": pid, "repeat_ms": round(second.ms)}
        raise Fail("no successful llm explanation was available to test idempotency on")

    def a9(self):
        for method, path, what in (("GET", "/v1/patients/P999/snapshot", "unknown patient snapshot"), ("POST", "/v1/patients/P999/analyses", "unknown patient analyze"),
                                   ("POST", "/v1/patients/P001/analyses/AN-P001-999999/explanation", "unknown analysis explain"),
                                   ("GET", "/v1/patients/P001/documents/does-not-exist", "unknown document")):
            r = self.call(method, path)
            expect(r.status == 404, f"{what} ({method} {path}) -> HTTP {r.status}, expected 404")

    def a10(self):
        r = self.call("OPTIONS", "/v1/patients/P001/analyses", headers={"Origin": ORIGIN, "Access-Control-Request-Method": "POST",
                                                                          "Access-Control-Request-Headers": "content-type"})
        h = {k.lower(): v for k, v in r.headers.items()}
        expect(r.status == 200 and h.get("access-control-allow-origin") == ORIGIN, f"preflight -> {r.status} {h.get('access-control-allow-origin')}")
        bad = self.call("GET", "/v1/health", headers={"Origin": "https://evil.example"})
        expect("access-control-allow-origin" not in {k.lower() for k in bad.headers}, "a foreign origin was allowed")

    def a11(self):
        """Two separate properties, both asserted and both reported with numbers:
        (i) sustained overload is throttled by the STAGE limit (429s appear, never a 5xx);
        (ii) a concurrent burst never produces a 5xx either (a Lambda concurrency limit would show up here as HTTP 500)."""
        from concurrent.futures import ThreadPoolExecutor

        def tally(replies):
            for r in replies:
                self.gateway_rejected += self.gateway_only(r)
                self.reached_lambda += not self.gateway_only(r)
            return [r.status for r in replies]

        sustained = tally([self.t.call("GET", "/v1/health") for _ in range(self.sustained)])  # as fast as the connection allows, no pacing
        with ThreadPoolExecutor(max_workers=self.burst) as pool:
            burst = tally(list(pool.map(lambda _: self.t.call("GET", "/v1/health"), range(self.burst))))
        detail = {"sustained": {"requests": len(sustained), "ok": sustained.count(200), "throttled_429": sustained.count(429), "5xx": sum(s >= 500 for s in sustained)},
                  "concurrent_burst": {"requests": len(burst), "ok": burst.count(200), "throttled_429": burst.count(429), "5xx": sum(s >= 500 for s in burst)}}
        problems = []
        if not set(sustained) <= {200, 429}:
            problems.append(f"sustained load produced {sorted(set(sustained) - {200, 429})}")
        if 429 not in sustained:
            problems.append("sustained overload was never throttled: the stage limit does not appear to be in effect")
        if 200 not in sustained:
            problems.append("every sustained request was throttled")
        if any(s >= 500 for s in burst):
            problems.append(f"a burst of {len(burst)} concurrent requests produced {sum(s >= 500 for s in burst)} HTTP 5xx (statuses {sorted(set(burst))}); "
                            "throttling must answer 429, never 5xx (suspect: the Lambda concurrency limit of this account)")
        expect(not problems, "; ".join(problems) + f" | measured: {json.dumps(detail)}")
        return detail

    def a12(self):
        by_route: dict[str, list[float]] = {}
        for route, ms in self.latencies:
            by_route.setdefault(route, []).append(ms)
        worst = max(ms for _, ms in self.latencies)
        expect(worst < 29000, f"a call took {worst:.0f} ms (API Gateway caps at 29 s)")
        summary = {r: {"n": len(v), "p50": int(statistics.median(v)), "max": int(max(v))} for r, v in by_route.items()}
        first_explain = next((ms for route, ms in self.latencies if route.endswith("explanation")), None)
        return {"max_ms": int(worst), "first_explain_ms": int(first_explain) if first_explain else None, "routes": summary}

    def fetch_log_events(self) -> list[str]:
        events, token = [], None
        while True:
            kw = dict(logGroupName=self.log_group, startTime=self.started_ms - 60_000, limit=1000)
            if token:
                kw["nextToken"] = token
            page = self.logs.filter_log_events(**kw)
            events += [e["message"] for e in page["events"]]
            token = page.get("nextToken")
            if not token or len(events) > 20000:
                return events

    @staticmethod
    def access_lines(events: list[str]) -> list[dict]:
        """Lambda prefixes app log lines with "[INFO]\t<time>\t<request id>\t"; the access line is the JSON after it."""
        out = []
        for m in events:
            body = m.rsplit("\t", 1)[-1].strip()
            if body.startswith('{"method"'):
                try:
                    out.append(json.loads(body))
                except json.JSONDecodeError:
                    raise Fail("an access-log line is not valid JSON")
        return out

    def a13(self):
        made = self.reached_lambda  # every request that reached the Lambda must be visible before the scan means anything
        names = [p["name"] for p in self.ok("GET", "/v1/patients")["patients"]]
        forbidden = names + [t for t in self.probe_text if len(t) >= 20] + ["aws_secret_access_key", "Signature=", "AKIA"]
        for _ in range(13):  # CloudWatch delivery lags the invocation by a few seconds
            events = self.fetch_log_events()
            access = self.access_lines(events)
            if len(access) >= made + 1:  # + this check's own patient-list request
                break
            self.sleep(5)
        expect(events, "no log events found in the Lambda log group for this run")
        expect(access, "no access-log lines found")
        expect(len(access) >= made + 1, f"only {len(access)} of the {made + 1} requests of this run reached the log group after waiting; the scan would be incomplete")
        for needle in forbidden:
            hits = [m for m in events if needle in m]
            expect(not hits, f"log line contains {'a patient name' if needle in names else 'clinical/model text or a credential marker'}: {needle[:12]}...")
        wrong = [sorted(a) for a in access if set(a) != ACCESS_LINE_FIELDS]
        expect(not wrong, f"access-log lines must contain exactly {sorted(ACCESS_LINE_FIELDS)}; saw {wrong[0] if wrong else ''}")
        return {"log_events_scanned": len(events), "access_lines": len(access), "requests_that_reached_lambda_before_scan": made, "answered_by_api_gateway_only": self.gateway_rejected}

    def a14(self):
        hl = self.healthlake
        totals = {t: int(hl.get_json(t, [("_total", "accurate"), ("_count", "1")])["total"]) for t in EXPECTED_COUNTS}
        expect(totals == EXPECTED_COUNTS, f"counts {totals}")
        for t in ("DetectedIssue", "RiskAssessment"):
            expect(int(hl.get_json(t, [("_total", "accurate"), ("_count", "1")])["total"]) == 0, f"{t} resources exist: Phase 4 must not write")
        bad = []
        for rtype in EXPECTED_COUNTS:
            for e in hl.search_entries(rtype, [("_count", "100")]):
                if e["resource"]["meta"].get("versionId") != "1":
                    bad.append(f"{rtype}/{e['resource']['id']}")
        expect(not bad, f"resources changed since import: {bad[:5]}")
        return {"counts": totals, "all_versionId_1": True, "DetectedIssue": 0, "RiskAssessment": 0}

    def a15(self):
        allowed_sids = {"BaseAccess": {"healthlake:ReadResource", "healthlake:SearchWithGet", "kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey",
                                       "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query", "logs:CreateLogStream", "logs:PutLogEvents"}}
        seen_hl = set()
        for name in self.iam.list_role_policies(RoleName=self.role_name)["PolicyNames"]:
            doc = self.iam.get_role_policy(RoleName=self.role_name, PolicyName=name)["PolicyDocument"]
            for st in doc["Statement"]:
                acts = [st["Action"]] if isinstance(st["Action"], str) else st["Action"]
                expect(not any(a == "*" or a.endswith(":*") for a in acts), f"wildcard action in {name}")
                expect(not any(a.startswith("bedrock-mantle:") for a in acts), "bedrock-mantle permission present")
                expect(not any(a.startswith("aws-marketplace:") for a in acts), "Marketplace permission present")
                expect({a for a in acts if a.startswith("bedrock:")} <= BEDROCK_ALLOWED_ACTIONS, f"unexpected Bedrock actions: {sorted(a for a in acts if a.startswith('bedrock:'))}")
                expect(not any(a.startswith("kms:") and "dynamodb" in json.dumps(st) for a in acts), "DynamoDB-related KMS permission present")
                seen_hl |= {a for a in acts if a.startswith("healthlake:")}
        expect(seen_hl == HEALTHLAKE_READ_ACTIONS, f"HealthLake actions on the role: {sorted(seen_hl)}")
        env = self.lambda_client.get_function_configuration(FunctionName=self.function)["Environment"]["Variables"]
        expect(env.get("HEALTHLAKE_WRITE_OUTPUTS") == "false", f"HEALTHLAKE_WRITE_OUTPUTS={env.get('HEALTHLAKE_WRITE_OUTPUTS')}")
        flagged = [k for k in env if SECRET_LIKE.search(k) and k not in SECRET_LIKE_ALLOWED_NAMES]
        expect(not flagged, f"secret-like environment variable on the function: {flagged}")
        if "OPENAI_API_KEY_SECRET_ARN" in env:
            expect(env["OPENAI_API_KEY_SECRET_ARN"].startswith("arn:aws:secretsmanager:"), "OPENAI_API_KEY_SECRET_ARN is not an ARN")
        expect("OPENAI_API_KEY" not in env, "raw OPENAI_API_KEY is a Lambda env var -- must only ever be the _SECRET_ARN pointer")
        return {"healthlake_actions": sorted(seen_hl), "write_outputs": "false"}

    def a16(self):
        return {"note": "browser smoke is run separately (local Vite with VITE_API_BASE_URL); see the report"}

    # -- driver
    def run_all(self, only: set[str] | None = None) -> dict:
        started = datetime.now(timezone.utc).isoformat(timespec="seconds")
        http = self.t.name in ("http", "inprocess")
        plan = [
            ("A0", "health", self.a0, True, ""), ("A1", "undocumented routes are not exposed", self.a1, self.t.name == "http", "API Gateway only"),
            ("A2", "patient list (10)", self.a2, True, ""), ("A3", "snapshots equal the local backend", self.a3, True, ""),
            ("A4", "documents equal the frozen notes", self.a4, True, ""),
            ("F1", "labelled fallback served correctly from the stored analysis (no model call)", self.f1, self.check_fallback, "not requested"), ("A5", "analyze all ten: golden + local parity", self.a5, True, ""),
            ("A6", "analyses persisted, numbering, table encryption", self.a6, True, ""),
            ("A7", "explain all ten x runs: grounded or labelled fallback; acceptance", self.a7, True, ""),
            ("A8", "explain is idempotent for a stored success", self.a8, True, ""), ("A9", "unknown ids -> 404", self.a9, True, ""),
            ("A10", "CORS preflight for the local UI origin", self.a10, True, ""), ("A11", "throttling answers 429, never 5xx", self.a11, self.t.name == "http", "API Gateway only"),
            ("A12", "latency (all < 29 s)", self.a12, True, ""), ("A13", "log hygiene", self.a13, bool(self.logs and self.log_group), "no logs adapter"),
            ("A14", "HealthLake unchanged (counts, versions, no derived resources)", self.a14, bool(self.healthlake), "no HealthLake adapter"),
            ("A15", "write-safety of the deployed role/config", self.a15, bool(self.iam and self.role_name and self.lambda_client), "no IAM/Lambda adapter"),
            ("A16", "browser smoke (manual/separate)", self.a16, True, ""),
        ]
        for cid, name, fn, needs, why in plan:
            if only and cid not in only:
                continue
            if cid in self.pending:  # never executed, never counted as a pass: the acceptance criteria stay exactly as they are
                self.results.append({"id": cid, "name": name, "status": "PENDING", "detail": {"reason": self.pending[cid]}})
                print(f"{cid:>4} PEND {name}  ({self.pending[cid]})", flush=True)
                continue
            self.run(cid, name, fn, needs=needs, why=why)
        failed = [r["id"] for r in self.results if r["status"] == "FAIL"]
        return {"startedAt": started, "finishedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"), "transport": self.t.name,
                "passed": sum(r["status"] == "PASS" for r in self.results), "skipped": [r["id"] for r in self.results if r["status"] == "SKIP"],
                "pending": {r["id"]: r["detail"]["reason"] for r in self.results if r["status"] == "PENDING"},
                "failed": failed, "explain": self.explain_stats, "requests": len(self.latencies), "throttled_retries": self.throttled_retries, "gateway_only_answers": self.gateway_rejected,
                "results": self.results}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=["http", "lambda"], required=True)
    ap.add_argument("--base-url")
    ap.add_argument("--function", default="medsafety-api")
    ap.add_argument("--table", default="medsafety-app-state")
    ap.add_argument("--log-group", default="/aws/lambda/medsafety-api")
    ap.add_argument("--role-name", default="MedSafetyApiLambdaRole")
    ap.add_argument("--datastore-id")
    ap.add_argument("--verifier-role-arn")
    ap.add_argument("--explain-runs", type=int, default=3)
    ap.add_argument("--checks", help="comma-separated ids, e.g. A0,A2,A5")
    ap.add_argument("--check-fallback", action="store_true", help="F1: verify the stored labelled fallback (needs one earlier failed explanation attempt; makes no model call)")
    ap.add_argument("--pending", help="comma-separated ids reported as PENDING (not run), e.g. A7,A8")
    ap.add_argument("--pending-reason", default="depends on an external service that is not enabled yet")
    ap.add_argument("--out", default="api_verify_report.json")
    args = ap.parse_args()

    import boto3

    region = "us-east-1"
    transport = HttpTransport(args.base_url) if args.mode == "http" else LambdaTransport(args.function, region)
    if args.mode == "http" and not args.base_url:
        ap.error("--base-url is required for --mode http")
    healthlake = None
    if args.datastore_id and args.verifier_role_arn:
        from app.repository.healthlake_client import HealthLakeClient

        healthlake = HealthLakeClient(args.datastore_id, region=region, role_arn=args.verifier_role_arn, session_name="medsafety-api-verify")
    suite = Suite(transport, explain_runs=args.explain_runs, dynamodb=boto3.client("dynamodb", region_name=region), table=args.table,
                  logs=boto3.client("logs", region_name=region), log_group=args.log_group, iam=boto3.client("iam"), role_name=args.role_name,
                  lambda_client=boto3.client("lambda", region_name=region), function=args.function, healthlake=healthlake,
                  pending={c: args.pending_reason for c in args.pending.split(",")} if args.pending else None, check_fallback=args.check_fallback)
    report = suite.run_all(set(args.checks.split(",")) if args.checks else None)
    Path(args.out).write_text(json.dumps(report, indent=2, default=list))
    print(f"\n{report['passed']} passed, {len(report['failed'])} failed, {len(report['skipped'])} skipped, {len(report['pending'])} pending; {report['requests']} requests; report: {args.out}")
    return 0 if not report["failed"] else 1


if __name__ == "__main__":
    sys.exit(main())
