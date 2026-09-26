"""scripts/api_verify.py (A0-A16) run in-process against the Lambda handler on the HealthLake + DynamoDB fakes -- and its ability to
DETECT the failures each check exists for (drift, writes, leaks, over-broad role, low guard acceptance)."""
from __future__ import annotations

import importlib.util
import json
import logging
import sys

import pytest

from app.api.deps import get_container
from app.container import build_container
from app.lambda_handler import handler
from app.main import app
from app.repository.app_state_dynamodb import DynamoAppStateStore
from app.repository.healthlake import HealthLakeFHIRRepository
from app.repository.healthlake_client import HealthLakeClient
from app.services.explanation.base import ExplanationService
from app.services.explanation.mock import MockExplanationService
from app.terminology import Terminology
from tests.conftest import PACKAGE_DIR
from tests.support.fake_dynamodb import TABLE, FakeDynamoDB
from tests.support.fake_healthlake import APP_KEY, DATASTORE_ID, VERIFIER_KEY, FakeHealthLake, credentials
from app.config import REPO_ROOT

spec = importlib.util.spec_from_file_location("api_verify", REPO_ROOT / "scripts" / "api_verify.py")
av = importlib.util.module_from_spec(spec)
sys.modules["api_verify"] = av  # dataclasses resolve their module through sys.modules
spec.loader.exec_module(av)
MODEL = av.EXPECTED_MODEL


class LlmLike(ExplanationService):
    """Stands in for a Bedrock-backed explainer: a grounded llm explanation, except for the runs listed in `fallback_when`."""

    def __init__(self, fallback_when=lambda n: False, code="EXPLANATION_REJECTED"):
        self.n, self.fallback_when, self.code, self.mock = 0, fallback_when, code, MockExplanationService()

    def explain(self, snapshot, analysis, note_context=None):
        self.n += 1
        if self.fallback_when(self.n):
            return self.mock.explain(snapshot, analysis, note_context, fallback_code=self.code)
        return self.mock.explain(snapshot, analysis, note_context).model_copy(update={"mode": "llm", "model": MODEL})


class FakeLogs:
    def __init__(self, records):
        self.records = records

    def filter_log_events(self, **kw):
        return {"events": [{"message": m} for m in self.records()]}


class FakeIam:
    def __init__(self, statements):
        self.doc = {"Statement": statements}

    def list_role_policies(self, RoleName):
        return {"PolicyNames": ["BaseAccess"]}

    def get_role_policy(self, RoleName, PolicyName):
        return {"PolicyDocument": self.doc}


class FakeLambda:
    def __init__(self, env):
        self.env = env

    def get_function_configuration(self, FunctionName):
        return {"Environment": {"Variables": self.env}}


class FakeDynamoAdmin:
    def __init__(self, table):
        self.table = table

    def describe_table(self, TableName):
        return {"Table": self.table}


GOOD_STATEMENTS = [
    {"Action": ["healthlake:ReadResource", "healthlake:SearchWithGet", "healthlake:GetCapabilities"], "Resource": "arn"},
    {"Action": ["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"], "Resource": "key", "Condition": {"StringEquals": {"kms:ViaService": "healthlake.us-east-1.amazonaws.com"}}},
    {"Action": ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query"], "Resource": "table"},
    {"Action": ["logs:CreateLogStream", "logs:PutLogEvents"], "Resource": "lg"},
    {"Action": ["bedrock:InvokeModel"], "Resource": "profile"}, {"Action": "bedrock:GetInferenceProfile", "Resource": "profile"},  # Gate B statements
]
GOOD_TABLE = {"TableStatus": "ACTIVE", "BillingModeSummary": {"BillingMode": "PAY_PER_REQUEST"}, "KeySchema": [{"AttributeName": "pk"}, {"AttributeName": "sk"}]}


@pytest.fixture()
def world(settings, caplog):
    caplog.set_level(logging.DEBUG)
    fake_hl, fake_db = FakeHealthLake(page_size=50), FakeDynamoDB()

    def container_with(explainer):
        client = HealthLakeClient(DATASTORE_ID, credentials=credentials(APP_KEY), transport=fake_hl.transport(), sleep=lambda s: None, max_retries=1)
        repo = HealthLakeFHIRRepository(client, Terminology.load(PACKAGE_DIR), DynamoAppStateStore(TABLE, client=fake_db))
        app.dependency_overrides[get_container] = lambda: build_container(settings, repository=repo, explainer=explainer)

    container_with(LlmLike())
    verifier = HealthLakeClient(DATASTORE_ID, credentials=credentials(VERIFIER_KEY), transport=fake_hl.transport(), sleep=lambda s: None)
    yield {"hl": fake_hl, "db": fake_db, "use": container_with, "verifier": verifier, "logs": lambda: [r.getMessage() for r in caplog.records]}
    app.dependency_overrides.clear()


def suite(world, **kw):
    defaults = dict(explain_runs=1, dynamodb=FakeDynamoAdmin(GOOD_TABLE), table=TABLE, logs=FakeLogs(world["logs"]), log_group="lg",
                    iam=FakeIam(GOOD_STATEMENTS), role_name="MedSafetyApiLambdaRole", lambda_client=FakeLambda({"HEALTHLAKE_WRITE_OUTPUTS": "false", "DATA_BACKEND": "healthlake"}),
                    function="medsafety-api", healthlake=world["verifier"], sleep=lambda seconds: None)  # never really sleep in unit tests
    return av.Suite(av.InProcessTransport(handler), **{**defaults, **kw})


def statuses(report):
    return {r["id"]: r["status"] for r in report["results"]}


def test_a_healthy_stack_passes_every_applicable_check(world):
    report = suite(world).run_all()
    assert report["failed"] == [], [r for r in report["results"] if r["status"] == "FAIL"]
    st = statuses(report)
    assert st["A5"] == st["A14"] == st["A15"] == st["A13"] == st["A7"] == "PASS"
    assert {cid for cid, s in st.items() if s == "SKIP"} == {"A1", "A11", "F1"}  # API-Gateway-only checks and the opt-in fallback check are SKIP, never PASS
    assert report["explain"]["acceptance"] == 1.0 and report["explain"]["models"] == [MODEL]


def test_checks_that_need_an_adapter_report_skip_not_pass(world):
    report = av.Suite(av.InProcessTransport(handler), explain_runs=1).run_all({"A13", "A14", "A15"})
    assert statuses(report) == {"A13": "SKIP", "A14": "SKIP", "A15": "SKIP"}


def test_a_direct_lambda_style_subset_can_be_selected(world):
    report = suite(world).run_all({"A0", "A2", "A5"})
    assert statuses(report) == {"A0": "PASS", "A2": "PASS", "A5": "PASS"}


# ---- each check detects what it is for -------------------------------------------------------------------
def test_a5_detects_deterministic_drift_from_the_golden_expectations(world):
    class Drifting(av.InProcessTransport):
        def call(self, method, path, body=None, headers=None):
            r = super().call(method, path, body, headers)
            if method == "POST" and path == "/v1/patients/P006/analyses" and isinstance(r.body, dict):
                r.body["dataGaps"] = []  # the approved P006 override says DG-001 must be there
            return r

    s = av.Suite(Drifting(handler), explain_runs=1)
    assert statuses(s.run_all({"A5"}))["A5"] == "FAIL"


def test_a14_detects_a_modified_frozen_resource_and_any_derived_resource(world):
    world["hl"]._commit(dict(world["hl"].current("Observation", "obs-p001-01"), valueString="tampered"))
    assert statuses(suite(world).run_all({"A14"}))["A14"] == "FAIL"


def test_a14_detects_a_written_detected_issue(world):
    world["hl"]._commit({"resourceType": "DetectedIssue", "id": "di-x", "status": "final"})
    report = suite(world).run_all({"A14"})
    (a14,) = report["results"]
    assert a14["status"] == "FAIL" and "DetectedIssue" in a14["detail"]["reason"]


@pytest.mark.parametrize("mutation,needle", [
    (lambda st: st.append({"Action": ["healthlake:CreateResource"], "Resource": "arn"}), "HealthLake actions"),
    (lambda st: st.append({"Action": ["healthlake:*"], "Resource": "arn"}), "wildcard"),
    (lambda st: st.append({"Action": ["bedrock-mantle:CreateInference"], "Resource": "arn"}), "bedrock-mantle"),
    (lambda st: st.append({"Action": ["bedrock:InvokeModelWithResponseStream"], "Resource": "arn"}), "unexpected Bedrock actions"),
    (lambda st: st.append({"Action": ["aws-marketplace:Subscribe"], "Resource": "*"}), "Marketplace"),
    (lambda st: st.append({"Action": ["kms:Decrypt"], "Resource": "key", "Condition": {"StringEquals": {"kms:ViaService": "dynamodb.us-east-1.amazonaws.com"}}}), "DynamoDB-related KMS"),
])
def test_a15_detects_an_over_broad_lambda_role(world, mutation, needle):
    statements = json.loads(json.dumps(GOOD_STATEMENTS))
    mutation(statements)
    (a15,) = suite(world, iam=FakeIam(statements)).run_all({"A15"})["results"]
    assert a15["status"] == "FAIL" and needle in a15["detail"]["reason"]


def test_a15_accepts_the_real_function_environment_including_the_max_tokens_limit(world):
    env = {"HEALTHLAKE_WRITE_OUTPUTS": "false", "BEDROCK_MAX_TOKENS": "4000", "BEDROCK_STRUCTURED_OUTPUT": "tool", "EXPLANATION_MODEL": "us.amazon.nova-2-lite-v1:0"}
    (a,) = suite(world, lambda_client=FakeLambda(env)).run_all({"A15"})["results"]
    assert a["status"] == "PASS", a
    for secret in ("AWS_SECRET_ACCESS_KEY", "ANTHROPIC_API_KEY", "SOME_TOKEN", "DB_PASSWORD"):
        (b,) = suite(world, lambda_client=FakeLambda({**env, secret: "x"})).run_all({"A15"})["results"]
        assert b["status"] == "FAIL" and "secret-like" in b["detail"]["reason"], secret


def test_a15_detects_write_outputs_enabled_and_secret_like_env_vars(world):
    (a,) = suite(world, lambda_client=FakeLambda({"HEALTHLAKE_WRITE_OUTPUTS": "true"})).run_all({"A15"})["results"]
    assert a["status"] == "FAIL" and "HEALTHLAKE_WRITE_OUTPUTS" in a["detail"]["reason"]
    (b,) = suite(world, lambda_client=FakeLambda({"HEALTHLAKE_WRITE_OUTPUTS": "false", "ANTHROPIC_API_KEY": "x"})).run_all({"A15"})["results"]
    assert b["status"] == "FAIL" and "secret-like" in b["detail"]["reason"]


def test_a15_accepts_the_openai_secret_arn_pointer_but_never_a_raw_openai_key(world):
    """Regression: found on a real deployed Lambda (docs/adr/0011) -- OPENAI_API_KEY_SECRET_ARN legitimately
    contains "KEY"/"SECRET" in its NAME (an ARN pointer, not a credential), which the SECRET_LIKE name-scan
    used to flag as a false positive. Its VALUE must still look like an ARN, and the raw OPENAI_API_KEY name
    must still be rejected if it ever appears."""
    base = {"HEALTHLAKE_WRITE_OUTPUTS": "false"}
    good = {**base, "OPENAI_API_KEY_SECRET_ARN": "arn:aws:secretsmanager:us-east-1:123456789012:secret:medsafety/openai-api-key-abc123"}
    (a,) = suite(world, lambda_client=FakeLambda(good)).run_all({"A15"})["results"]
    assert a["status"] == "PASS", a

    (b,) = suite(world, lambda_client=FakeLambda({**base, "OPENAI_API_KEY_SECRET_ARN": "sk-not-an-arn-at-all"})).run_all({"A15"})["results"]
    assert b["status"] == "FAIL" and "not an ARN" in b["detail"]["reason"]

    (c,) = suite(world, lambda_client=FakeLambda({**good, "OPENAI_API_KEY": "sk-a-real-looking-key"})).run_all({"A15"})["results"]
    assert c["status"] == "FAIL" and "OPENAI_API_KEY" in c["detail"]["reason"]  # caught by the general secret-like scan (not in the allow-list)


def test_a13_detects_patient_names_or_note_text_in_the_logs(world):
    name = av.InProcessTransport(handler).call("GET", "/v1/patients").body["patients"][0]["name"]
    leaky = lambda: world["logs"]() + [f"processing request for {name}"]
    (a13,) = suite(world, logs=FakeLogs(leaky)).run_all({"A13"})["results"]
    assert a13["status"] == "FAIL" and "patient name" in a13["detail"]["reason"]


def test_a13_needs_real_log_events_and_access_lines(world):
    (a13,) = suite(world, logs=FakeLogs(lambda: [])).run_all({"A13"})["results"]
    assert a13["status"] == "FAIL" and "no log events" in a13["detail"]["reason"]


def test_a6_detects_a_table_that_is_not_using_default_encryption(world):
    table = {**GOOD_TABLE, "SSEDescription": {"Status": "ENABLED", "SSEType": "KMS"}}
    (a6,) = suite(world, dynamodb=FakeDynamoAdmin(table)).run_all({"A6"})["results"]
    assert a6["status"] == "FAIL" and "default AWS-owned encryption" in a6["detail"]["reason"]


# ---- guard acceptance floor -------------------------------------------------------------------------------
def test_a7_passes_at_the_phase2_level_and_stops_below_the_floor(world):
    world["use"](LlmLike(fallback_when=lambda n: n % 7 == 0))  # 4 of 30 runs fall back -> 26/30 = 87 %
    report = suite(world, explain_runs=3).run_all({"A7"})
    assert statuses(report)["A7"] == "PASS" and 0.8 <= report["explain"]["acceptance"] < 1.0
    world["use"](LlmLike(fallback_when=lambda n: n % 4 == 0))  # 25 % fallbacks -> 75 % < 80 % floor
    (a7,) = suite(world, explain_runs=3).run_all({"A7"})["results"]
    assert a7["status"] == "FAIL" and "STOP and report" in a7["detail"]["reason"]


def test_a7_rejects_an_unexpected_model_a_missing_public_code_and_a_non_200(world):
    world["use"](LlmLike())
    (a,) = suite(world, expected_model="anthropic.claude-opus-5").run_all({"A7"})["results"]
    assert a["status"] == "FAIL" and "answered by" in a["detail"]["reason"]
    world["use"](LlmLike(fallback_when=lambda n: True, code="SOMETHING_INTERNAL"))
    (b,) = suite(world).run_all({"A7"})["results"]
    assert b["status"] == "FAIL"


def test_a8_requires_a_stored_success_to_be_returned_unchanged(world):
    world["use"](LlmLike(fallback_when=lambda n: True))
    (a8,) = suite(world).run_all({"A8"})["results"]
    assert a8["status"] == "FAIL" and "no successful llm explanation" in a8["detail"]["reason"]


def test_a1_and_a11_run_against_http_transports_only(world):
    class Gateway:
        name = "http"

        def __init__(self, throttle_after=10, burst_5xx=False):
            self.n, self.throttle_after, self.burst_5xx = 0, throttle_after, burst_5xx

        def call(self, method, path, body=None, headers=None):
            if path in ("/docs", "/openapi.json", "/redoc", "/"):
                return av.Reply(403, {"message": "Missing Authentication Token"}, {}, 1.0)
            self.n += 1
            if self.burst_5xx and self.n > 40:  # the concurrent burst (after the sustained part) hits a Lambda concurrency limit
                return av.Reply(500, {"message": "Internal server error"}, {}, 1.0)
            return av.Reply(429, {"message": "Too Many Requests"}, {}, 1.0) if self.n % 50 > self.throttle_after or self.n > 40 and self.throttle_after < 10**5 and self.n % 3 == 0 else av.Reply(200, {"status": "ok"}, {}, 1.0)

    s = av.Suite(Gateway(), burst=20, sustained=40)
    s.sleep = lambda x: None
    (a11,) = s.run_all({"A11"})["results"]
    assert a11["status"] == "PASS" and a11["detail"]["sustained"]["throttled_429"] > 0 and a11["detail"]["concurrent_burst"]["5xx"] == 0
    (a1,) = av.Suite(Gateway()).run_all({"A1"})["results"]
    assert a1["status"] == "PASS"
    never = av.Suite(Gateway(throttle_after=10**6), burst=5, sustained=30)
    (bad,) = never.run_all({"A11"})["results"]
    assert bad["status"] == "FAIL" and "never throttled" in bad["detail"]["reason"]   # a limit that never triggers is not verified


def test_a11_reports_a_concurrent_burst_that_produces_5xx_as_a_failure_with_the_numbers(world):
    """Regression for the live finding: 40 concurrent requests -> HTTP 500 (suspected Lambda concurrency limit) while sustained load is throttled correctly."""
    class Burst500:
        name = "http"

        def __init__(self):
            self.n = 0

        def call(self, method, path, body=None, headers=None):
            self.n += 1
            if self.n <= 30:      # sustained phase: throttled correctly
                return av.Reply(429 if self.n > 10 else 200, {"message": "Too Many Requests"}, {}, 1.0)
            return av.Reply(500 if self.n % 4 == 0 else 200, {"message": "Internal server error"}, {}, 1.0)

    s = av.Suite(Burst500(), burst=12, sustained=30)
    (a11,) = s.run_all({"A11"})["results"]
    assert a11["status"] == "FAIL"
    assert "concurrent requests produced" in a11["detail"]["reason"] and "Lambda concurrency limit" in a11["detail"]["reason"] and '"sustained"' in a11["detail"]["reason"]
    assert "sustained load produced" not in a11["detail"]["reason"]   # the sustained property itself was fine


def test_apigateway_own_5xx_is_not_demanded_from_the_lambda_log(world):
    r = av.Reply(500, {"message": "Internal server error"}, {}, 1.0)
    assert av.Suite.gateway_only(r) and av.Suite.gateway_only(av.Reply(502, {"message": "Internal server error"}, {}, 1.0))
    assert not av.Suite.gateway_only(av.Reply(500, "Internal Server Error", {}, 1.0))   # the app's own 500 is plain text and stays a real failure signal


def test_gateway_throttling_outside_a11_is_paced_and_retried_not_reported_as_an_app_failure(world):
    class Flaky:
        name = "http"

        def __init__(self, fails):
            self.fails, self.calls = fails, 0

        def call(self, method, path, body=None, headers=None):
            self.calls += 1
            return av.Reply(429, {"message": "Too Many Requests"}, {}, 1.0) if self.calls <= self.fails else av.Reply(404, {"detail": "Patient P999 not found"}, {}, 1.0)

    t = Flaky(2)
    s = av.Suite(t)
    s.sleep = lambda x: None
    assert s.call("POST", "/v1/patients/P999/analyses").status == 404
    assert (s.throttled_retries, s.gateway_rejected, s.reached_lambda) == (2, 2, 1)
    stuck = Flaky(99)
    s = av.Suite(stuck)
    s.sleep = lambda x: None
    (a9,) = s.run_all({"A9"})["results"]
    assert a9["status"] == "FAIL" and "HTTP 429" in a9["detail"]["reason"] and "expected 404" in a9["detail"]["reason"]  # the status is now in the message


def test_a13_expects_log_lines_only_for_requests_that_reached_the_lambda(world):
    """Regression: 87 requests, 4 answered by API Gateway itself (unknown routes) => 83 log lines is the complete set."""
    s = suite(world)
    s.gateway_rejected, s.reached_lambda = 4, 0
    for _ in range(3):
        s.call("GET", "/v1/health")
    s.gateway_rejected += 4  # requests made in the run that API Gateway answered on its own must not be demanded from the log
    (a13,) = s.run_all({"A13"})["results"]
    assert a13["status"] == "PASS" and a13["detail"]["answered_by_api_gateway_only"] == 8


def test_report_contains_no_clinical_text_or_model_output(world):
    s = suite(world)
    report = s.run_all()
    text = json.dumps(report, default=list)
    for needle in [p["name"] for p in av.InProcessTransport(handler).call("GET", "/v1/patients").body["patients"]] + s.probe_text:
        assert needle not in text


def test_a13_reads_access_lines_behind_the_lambda_log_prefix_and_rejects_extra_fields(world):
    prefix = "[INFO]\t2026-09-19T16:15:28.214Z\t4af635da-abdf-4325-bea8-4ddc5a5b6734\t"
    good = prefix + json.dumps({"method": "GET", "route": "/v1/patients", "status": 200, "ms": 12, "requestId": "r"})
    (ok,) = suite(world, logs=FakeLogs(lambda: [good, prefix + "healthlake GET Patient -> 200 rid=x 5ms"])).run_all({"A13"})["results"]
    assert ok["status"] == "PASS" and ok["detail"]["access_lines"] == 1
    extra = prefix + json.dumps({"method": "GET", "route": "/v1/patients/P001/snapshot", "status": 200, "ms": 1, "requestId": "r", "path": "/v1/patients/P001"})
    (bad,) = suite(world, logs=FakeLogs(lambda: [extra])).run_all({"A13"})["results"]
    assert bad["status"] == "FAIL" and "exactly" in bad["detail"]["reason"]
    (none,) = suite(world, logs=FakeLogs(lambda: [prefix + "healthlake GET Patient -> 200"])).run_all({"A13"})["results"]
    assert none["status"] == "FAIL" and "no access-log lines" in none["detail"]["reason"]


def test_a13_waits_for_delayed_log_delivery_and_fails_if_the_run_never_appears(world):
    """Regression: a scan over a half-delivered log (10 events for 54 requests) is meaningless and must not pass."""
    ticks = []

    class Lagging:
        def __init__(self, source, after):
            self.source, self.after = source, after

        def filter_log_events(self, **kw):
            ticks.append(1)
            return {"events": [{"message": m} for m in (self.source() if len(ticks) > self.after else [])]}

    s = suite(world, logs=Lagging(world["logs"], after=2))
    s.sleep = lambda seconds: None
    (ok,) = s.run_all({"A13"})["results"]
    assert ok["status"] == "PASS" and len(ticks) > 2  # it polled until the lines arrived
    ticks.clear()
    s = suite(world, logs=Lagging(world["logs"], after=10**6))
    s.sleep = lambda seconds: None
    s.call("GET", "/v1/health")
    (bad,) = s.run_all({"A13"})["results"]
    assert bad["status"] == "FAIL" and "no log events" in bad["detail"]["reason"]
    partial = FakeLogs(lambda: [json.dumps({"method": "GET", "route": "/v1/health", "status": 200, "ms": 1, "requestId": "r"})])
    s = suite(world, logs=partial)
    s.sleep = lambda seconds: None
    for _ in range(5):
        s.call("GET", "/v1/health")
    (short,) = s.run_all({"A13"})["results"]
    assert short["status"] == "FAIL" and "would be incomplete" in short["detail"]["reason"]


def test_pending_checks_are_reported_pending_are_never_executed_and_never_counted_as_passed(world):
    """A7/A8 depend on Bedrock; while it is not enabled they are PENDING -- not run (no model call), not PASS, and their code is untouched."""
    calls = []

    class Counting(av.InProcessTransport):
        def call(self, method, path, body=None, headers=None):
            calls.append((method, path))
            return super().call(method, path, body, headers)

    s = av.Suite(Counting(handler), explain_runs=1, pending={"A7": "bedrock not enabled", "A8": "bedrock not enabled"})
    report = s.run_all({"A0", "A2", "A5", "A7", "A8"})
    st = statuses(report)
    assert st == {"A0": "PASS", "A2": "PASS", "A5": "PASS", "A7": "PENDING", "A8": "PENDING"}
    assert report["pending"] == {"A7": "bedrock not enabled", "A8": "bedrock not enabled"} and report["passed"] == 3 and report["failed"] == []
    assert not any(path.endswith("/explanation") for _, path in calls)  # no explanation request was made at all


def test_the_acceptance_criteria_are_unchanged_by_the_pending_mechanism():
    assert av.ACCEPTANCE_FLOOR == 0.80 and av.ACCEPTANCE_BASELINE == 0.90 and av.EXPECTED_MODEL == "us.amazon.nova-2-lite-v1:0"
    # and A7 still fails a suite whose explanations are below the floor once it is no longer pending (covered by test_a7_passes_at_the_phase2_level...)


# ---- F1: the labelled fallback, verified without any model call -------------------------------------------------
def store_fallback(world, code="EXPLANATION_UNAVAILABLE"):
    world["use"](LlmLike(fallback_when=lambda n: True, code=code))
    t = av.InProcessTransport(handler)
    analysis = t.call("POST", "/v1/patients/P001/analyses").body
    assert t.call("POST", f"/v1/patients/P001/analyses/{analysis['analysisId']}/explanation").status == 200


def test_f1_passes_for_a_correctly_labelled_stored_fallback_and_makes_no_explanation_request(world):
    store_fallback(world)
    calls = []

    class Counting(av.InProcessTransport):
        def call(self, method, path, body=None, headers=None):
            calls.append((method, path))
            return super().call(method, path, body, headers)

    (f1,) = av.Suite(Counting(handler), check_fallback=True).run_all({"F1"})["results"]
    assert f1["status"] == "PASS" and f1["detail"]["fallbackCode"] == "EXPLANATION_UNAVAILABLE" and f1["detail"]["deterministic_result_unchanged"]
    assert calls == [("GET", "/v1/patients/P001/analyses/latest")]  # one read; no POST at all


def test_f1_is_off_unless_requested_and_skips_when_there_is_nothing_stored_to_inspect(world):
    (off,) = av.Suite(av.InProcessTransport(handler)).run_all({"F1"})["results"]
    assert off["status"] == "SKIP"
    av.InProcessTransport(handler).call("POST", "/v1/patients/P001/analyses")  # analysed, never explained
    (none,) = av.Suite(av.InProcessTransport(handler), check_fallback=True).run_all({"F1"})["results"]
    assert none["status"] == "SKIP" and "no fallback to inspect" in none["detail"]["reason"]   # nothing to inspect is never reported as a pass
    assert av.Suite(av.InProcessTransport(handler), check_fallback=True).run_all({"F1"})["failed"] == []


@pytest.mark.parametrize("leak", ["Bedrock rejected the request", "AccessDeniedException", "arn:aws:iam::1:role/x", "Your account is currently being verified", "Traceback (most recent call last)", "request id 123"])
def test_f1_detects_provider_detail_leaking_into_the_fallback(world, leak):
    store_fallback(world)

    class Leaky(av.InProcessTransport):
        def call(self, method, path, body=None, headers=None):
            r = super().call(method, path, body, headers)
            if isinstance(r.body, dict) and r.body.get("aiExplanation"):
                r.body["aiExplanation"]["text"] += " " + leak
            return r

    (f1,) = av.Suite(Leaky(handler), check_fallback=True).run_all({"F1"})["results"]
    assert f1["status"] == "FAIL" and "provider detail" in f1["detail"]["reason"]


def test_f1_detects_a_non_public_code_a_wrong_reason_and_a_real_llm_answer_posing_as_fallback(world):
    store_fallback(world)

    def tamper(mutate):
        class T(av.InProcessTransport):
            def call(self, method, path, body=None, headers=None):
                r = super().call(method, path, body, headers)
                if isinstance(r.body, dict) and r.body.get("aiExplanation"):
                    mutate(r.body["aiExplanation"])
                return r
        return av.Suite(T(handler), check_fallback=True).run_all({"F1"})["results"][0]["status"]

    assert tamper(lambda e: e.update(fallbackCode="ThrottlingException")) == "FAIL"
    assert tamper(lambda e: e.update(fallbackReason="boto3 error 400")) == "FAIL"
    assert tamper(lambda e: e.update(mode="llm", model="us.amazon.nova-2-lite-v1:0")) == "FAIL"
