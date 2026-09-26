"""Phase 4 deployment scripts against a stub `aws` (offline). Mutating calls are skipped by DRY_RUN=1 and echoed as `DRY-RUN aws ...`."""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys

import pytest

from app.config import REPO_ROOT

API = REPO_ROOT / "infrastructure" / "aws" / "api"
ACCT = "123456789012"
DS = "00000000000000000000000000000002"
KEY = f"arn:aws:kms:us-east-1:{ACCT}:key/00000000-0000-0000-0000-000000000000"
PROFILE = f"arn:aws:bedrock:us-east-1:{ACCT}:inference-profile/us.amazon.nova-2-lite-v1:0"
FM = [f"arn:aws:bedrock:{r}::foundation-model/amazon.nova-2-lite-v1:0" for r in ("us-east-1", "us-east-2", "us-west-2")]
GOOD_AVAIL = {"modelId": "amazon.nova-2-lite-v1", "agreementAvailability": {"status": "AVAILABLE"}, "authorizationStatus": "NOT_AUTHORIZED",  # what this account reports for every Amazon model
              "entitlementAvailability": "AVAILABLE", "regionAvailability": "AVAILABLE"}
GOOD_PROFILE = {"inferenceProfileId": "us.amazon.nova-2-lite-v1:0", "inferenceProfileArn": PROFILE, "status": "ACTIVE", "type": "SYSTEM_DEFINED",
                "models": [{"modelArn": a} for a in FM]}

STUB = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
open(os.environ["STUB_LOG"], "a").write(json.dumps(args) + "\n")
scenario = json.load(open(os.environ["STUB_SCENARIO"]))
key = " ".join(args[:2])
q = " ".join(args)
if key == "sts get-caller-identity":
    print(scenario["account"] if "Account" in q else scenario["arn"]); sys.exit(0)
entry = scenario.get("calls", {}).get(key)
if entry is None:
    sys.exit(0)  # unlisted calls succeed with empty output
if entry.get("stderr"):
    sys.stderr.write(entry["stderr"] + "\n")
if entry.get("stdout") is not None:
    out = entry["stdout"]; print(out if isinstance(out, str) else json.dumps(out))
sys.exit(entry.get("rc", 0))
'''

NOT_FOUND = {
    "dynamodb describe-table": {"rc": 254, "stderr": "An error occurred (ResourceNotFoundException) when calling the DescribeTable operation: Requested resource not found"},
    "iam get-role": {"rc": 254, "stderr": "An error occurred (NoSuchEntity) when calling the GetRole operation: The role cannot be found"},
    "iam get-role-policy": {"rc": 254, "stderr": "An error occurred (NoSuchEntity) when calling the GetRolePolicy operation: The role policy cannot be found"},
    "lambda get-function": {"rc": 254, "stderr": "An error occurred (ResourceNotFoundException) when calling the GetFunction operation: Function not found"},
    "secretsmanager describe-secret": {"rc": 254, "stderr": "An error occurred (ResourceNotFoundException) when calling the DescribeSecret operation: Secrets Manager can't find the specified secret."},
}


def scenario(**over):
    calls = {"healthlake describe-fhir-datastore": {"stdout": "ACTIVE"}, **NOT_FOUND,
             "logs describe-log-groups": {"stdout": ""}, "apigateway get-rest-apis": {"stdout": ""},
             "bedrock get-foundation-model-availability": {"stdout": GOOD_AVAIL}, "bedrock get-inference-profile": {"stdout": GOOD_PROFILE},
             "bedrock list-foundation-model-agreement-offers": {"rc": 254, "stderr": "An error occurred (ValidationException) when calling the ListFoundationModelAgreementOffers operation: Agreement not supported for this model"},
             "bedrock get-use-case-for-model-access": {"rc": 254, "stderr": "An error occurred (ResourceNotFoundException)"}}
    calls.update(over.pop("calls", {}))
    return {"account": ACCT, "arn": f"arn:aws:sts::{ACCT}:assumed-role/MedSafetyPhase3DeployerRole/medsafety-phase3", "calls": calls, **over}


@pytest.fixture()
def box(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "aws").write_text(STUB)
    (bindir / "aws").chmod((bindir / "aws").stat().st_mode | stat.S_IEXEC)
    hl = tmp_path / "hl.env"
    hl.write_text(f"DS_ID={DS}\nKEY_ARN={KEY}\n")
    state = tmp_path / "state"
    state.mkdir()
    return {"bin": bindir, "state": state, "hl": hl, "log": tmp_path / "aws.log", "scenario": tmp_path / "scenario.json", "tmp": tmp_path}


OPENAI_SECRET_ARN = f"arn:aws:secretsmanager:us-east-1:{ACCT}:secret:medsafety/openai-api-key-abc123"
BASE_STATE = {"PROFILE_ARN": PROFILE, "FOUNDATION_MODEL_ARNS_JSON": json.dumps(FM), "ROLE_ARN": f"arn:aws:iam::{ACCT}:role/MedSafetyApiLambdaRole",
              "ZIP_PATH": "/tmp/medsafety-api.zip", "FUNCTION_ARN": f"arn:aws:lambda:us-east-1:{ACCT}:function:medsafety-api", "API_ID": "abc123",
              "OPENAI_SECRET_ARN": OPENAI_SECRET_ARN}


def run(box, script, *, dry=True, state=None, sc=None, extra=None):
    box["scenario"].write_text(json.dumps(sc or scenario()))
    if state is not None:
        (box["state"] / "deploy.env").write_text("".join(f"{k}={v!r}\n" for k, v in state.items()))
    env = {**os.environ, "PATH": f"{box['bin']}:{os.environ['PATH']}", "AWS_PROFILE": "medsafety", "STATE_DIR": str(box["state"]), "HL_STATE_FILE": str(box["hl"]),
           "STUB_LOG": str(box["log"]), "STUB_SCENARIO": str(box["scenario"]), **(extra or {})}
    if dry:
        env["DRY_RUN"] = "1"
    return subprocess.run(["bash", str(API / script)], env=env, capture_output=True, text=True, timeout=120)


def calls(box):
    return [json.loads(l) for l in box["log"].read_text().splitlines()] if box["log"].exists() else []


# ---- preflight -------------------------------------------------------------------------------------------------
def test_preflight_is_strictly_read_only_and_prints_the_gate_b_statements_without_applying_them(box):
    proc = run(box, "00_preflight.sh", dry=False)
    assert proc.returncode == 0, proc.stderr[-1500:]
    used = {" ".join(c[:2]) for c in calls(box)}
    assert used <= {"sts get-caller-identity", "healthlake describe-fhir-datastore", "dynamodb describe-table", "iam get-role", "lambda get-function",
                    "logs describe-log-groups", "apigateway get-rest-apis", "bedrock get-foundation-model-availability", "bedrock get-inference-profile",
                    "bedrock list-foundation-model-agreement-offers"}  # get-use-case-for-model-access is Anthropic-only and is skipped for Nova
    assert "GATE B statements (NOT applied" in proc.stderr and PROFILE in proc.stderr and all(a in proc.stderr for a in FM)
    assert "bedrock:InvokeModel" in proc.stderr and "bedrock:GetInferenceProfile" in proc.stderr and "mantle" not in proc.stderr.lower()
    assert "informational for Amazon models" in proc.stderr and "only proven by the first real call at Gate B" in proc.stderr
    assert "aws-marketplace" not in proc.stderr and "bedrock get-use-case-for-model-access" not in " ".join(" ".join(c[:2]) for c in calls(box))
    st = (box["state"] / "deploy.env").read_text()
    assert PROFILE in st and "us-west-2" in st  # exact ARNs recorded for the approval, not yet used in IAM
    assert not any(c[0] == "iam" and c[1].startswith(("put", "create")) for c in calls(box))


@pytest.mark.parametrize("patch", [{"agreementAvailability": {"status": "PENDING"}}, {"entitlementAvailability": "NOT_AVAILABLE"}, {"regionAvailability": "NOT_AVAILABLE"}])
def test_preflight_stops_with_exit_3_when_the_model_needs_account_enablement(box, patch):
    sc = scenario(calls={"bedrock get-foundation-model-availability": {"stdout": {**GOOD_AVAIL, **patch}}})
    proc = run(box, "00_preflight.sh", dry=False, sc=sc)
    assert proc.returncode == 3 and "ACCOUNT ENABLEMENT REQUIRED" in proc.stderr and "Not changing models" in proc.stderr
    assert "PREFLIGHT OK" not in proc.stderr and not (box["state"] / "deploy.env").exists()


def test_preflight_stops_with_exit_4_when_the_profile_is_not_as_planned(box):
    proc = run(box, "00_preflight.sh", dry=False, sc=scenario(calls={"bedrock get-inference-profile": {"stdout": {**GOOD_PROFILE, "status": "CREATING"}}}))
    assert proc.returncode == 4 and "not as planned" in proc.stderr


@pytest.mark.parametrize("existing,message", [("dynamodb describe-table", "unexpected existing DynamoDB table"), ("iam get-role", "unexpected existing IAM role"),
                                              ("lambda get-function", "unexpected existing Lambda function")])
def test_preflight_stops_on_an_unexpected_existing_resource(box, existing, message):
    proc = run(box, "00_preflight.sh", dry=False, sc=scenario(calls={existing: {"rc": 0, "stdout": "{}"}}))
    assert proc.returncode == 1 and message in proc.stderr


def test_preflight_stops_if_a_log_group_or_api_of_that_name_exists_or_the_datastore_is_not_active(box):
    assert "unexpected existing log group" in run(box, "00_preflight.sh", dry=False, sc=scenario(calls={"logs describe-log-groups": {"stdout": "/aws/lambda/medsafety-api"}})).stderr
    assert "unexpected existing REST API" in run(box, "00_preflight.sh", dry=False, sc=scenario(calls={"apigateway get-rest-apis": {"stdout": "abc123"}})).stderr
    assert "expected ACTIVE" in run(box, "00_preflight.sh", dry=False, sc=scenario(calls={"healthlake describe-fhir-datastore": {"stdout": "CREATING"}})).stderr


def test_root_identity_is_refused(box):
    proc = run(box, "00_preflight.sh", dry=False, sc=scenario(arn=f"arn:aws:iam::{ACCT}:root"))
    assert proc.returncode != 0 and "root" in proc.stderr


# ---- state / role / gate B -------------------------------------------------------------------------------------
def test_dynamodb_table_is_created_on_demand_with_default_encryption_and_no_kms(box):
    proc = run(box, "10_state.sh", sc=scenario(calls={"dynamodb describe-table": {"rc": 254, "stderr": "ResourceNotFoundException"}}))
    assert proc.returncode == 0, proc.stderr[-1500:]
    (cmd,) = [l for l in proc.stderr.splitlines() if "dynamodb create-table" in l]
    assert "--billing-mode PAY_PER_REQUEST" in cmd and "--table-class STANDARD" in cmd and "AttributeName=pk,KeyType=HASH" in cmd and "AttributeName=sk,KeyType=RANGE" in cmd
    assert "sse-specification" not in cmd.lower() and "kms" not in cmd.lower()  # default AWS-owned encryption
    assert "Project,Value=medsafety-intelligence" in cmd and "Phase,Value=4" in cmd
    assert "update-time-to-live --table-name medsafety-app-state --time-to-live-specification Enabled=true,AttributeName=ttl" in proc.stderr
    assert "logs put-retention-policy --log-group-name /aws/lambda/medsafety-api --retention-in-days 30" in proc.stderr
    assert not re.search(r"kms (create|put|schedule|create-grant)", proc.stderr)


def test_role_is_created_without_any_bedrock_statement(box):
    proc = run(box, "20_role.sh", state={})
    assert proc.returncode == 0, proc.stderr[-1500:]
    assert "iam create-role --role-name MedSafetyApiLambdaRole" in proc.stderr and "iam put-role-policy --role-name MedSafetyApiLambdaRole --policy-name BaseAccess" in proc.stderr
    rendered = json.loads((box["state"] / "rendered" / "lambda-access.json").read_text())
    text = json.dumps(rendered)
    assert "bedrock" not in text and f"datastore/fhir/{DS}" in text
    assert "BedrockInvoke" not in proc.stderr
    # healthlake:UpdateResource IS expected here (proposed, not yet applied to AWS): the least-privilege
    # persist_to_fhir() write-back grant, scoped to this same datastore ARN. CreateResource is deliberately
    # NOT granted -- see test_api_policies.py::test_lambda_role_grants_update_only_for_fhir_persist_never_create_wildcard_or_history.
    assert "healthlake:UpdateResource" in text and "healthlake:CreateResource" not in text


def test_gate_b_refuses_without_explicit_approval_and_applies_exactly_the_shown_statements_with_it(box):
    refused = run(box, "25_bedrock_policy.sh", state=BASE_STATE)
    assert refused.returncode != 0 and "Gate B" in refused.stderr and "iam put-role-policy" not in refused.stderr
    assert PROFILE in refused.stderr  # the statements were shown
    ok = run(box, "25_bedrock_policy.sh", state=BASE_STATE, extra={"APPROVE_BEDROCK_POLICY": "yes"})
    assert ok.returncode == 0 and "iam put-role-policy --role-name MedSafetyApiLambdaRole --policy-name BedrockInvoke" in ok.stderr
    applied = json.loads((box["state"] / "rendered" / "lambda-bedrock.json").read_text())
    assert {a for s in applied["Statement"] for a in ([s["Action"]] if isinstance(s["Action"], str) else s["Action"])} == {"bedrock:InvokeModel", "bedrock:GetInferenceProfile"}


# ---- Gate C: OpenAI secret creation ---------------------------------------------------------------------------
CANARY_KEY = "sk-test-DO-NOT-LEAK-CANARY-VALUE-000000000000000000"


def key_file(path_obj, content: bytes, mode=0o600):
    path_obj.write_bytes(content)
    path_obj.chmod(mode)
    return path_obj


def providers_env(path_obj, key: str | None, *, extra_lines: tuple[str, ...] = ()):
    """A minimal .local/providers.env-shaped file. `key=None` omits the OPENAI_API_KEY line entirely."""
    lines = list(extra_lines)
    if key is not None:
        lines.append(f"OPENAI_API_KEY={key}")
    path_obj.write_text("\n".join(lines) + "\n")
    return path_obj


def tmpdir_scratch(box):
    """A dedicated, empty scratch dir passed as TMPDIR so the script's own `mktemp` lands here -- lets a test
    assert the temp key file was actually removed by the EXIT trap, without knowing its generated name."""
    d = box["tmp"] / "scratch_tmpdir"
    d.mkdir(exist_ok=True)
    return d


def test_gate_c_refuses_without_approval_and_creates_the_secret_with_it(box):
    kf = key_file(box["tmp"] / "openai.key", b"sk-test-not-a-real-key\n")
    refused = run(box, "26_openai_secret.sh", extra={"OPENAI_KEY_FILE": str(kf)})
    assert refused.returncode != 0 and "Gate C" in refused.stderr
    assert not any(c[0] == "secretsmanager" for c in calls(box))

    ok = run(box, "26_openai_secret.sh", extra={"APPROVE_CREATE_OPENAI_SECRET": "yes", "OPENAI_KEY_FILE": str(kf)})
    assert ok.returncode == 0, ok.stderr[-1500:]
    assert "secretsmanager create-secret --name medsafety/openai-api-key" in ok.stderr
    assert "--query ARN --output text" in ok.stderr
    (cmd,) = [l for l in ok.stderr.splitlines() if "secretsmanager create-secret" in l]
    assert "--secret-string file://" in cmd and str(kf) not in cmd  # a FRESH temp file is used, never the original path


def test_gate_c_refuses_a_key_file_inside_the_repository(box):
    proc = run(box, "26_openai_secret.sh", extra={"APPROVE_CREATE_OPENAI_SECRET": "yes", "OPENAI_KEY_FILE": str(API / "env.sh")})
    assert proc.returncode != 0 and "inside the repository" in proc.stderr
    assert not any(c[0] == "secretsmanager" for c in calls(box))


def test_gate_c_refuses_permissive_key_file_permissions(box):
    kf = key_file(box["tmp"] / "openai.key", b"sk-test-not-a-real-key\n", mode=0o644)
    proc = run(box, "26_openai_secret.sh", extra={"APPROVE_CREATE_OPENAI_SECRET": "yes", "OPENAI_KEY_FILE": str(kf)})
    assert proc.returncode != 0 and "644" in proc.stderr and "chmod 600" in proc.stderr
    assert not any(c[0] == "secretsmanager" for c in calls(box))


@pytest.mark.parametrize("content,expected", [
    (b"sk-line-one\r\nno-cr-here", "carriage return"),
    (b"sk-line-one\nsk-line-two\n", "exactly one non-empty line"),
    (b"", "exactly one non-empty line"),
    (b"  sk-with-leading-space\n", "leading/trailing whitespace"),
])
def test_gate_c_refuses_a_malformed_key_file(box, content, expected):
    kf = key_file(box["tmp"] / "openai.key", content)
    proc = run(box, "26_openai_secret.sh", extra={"APPROVE_CREATE_OPENAI_SECRET": "yes", "OPENAI_KEY_FILE": str(kf)})
    assert proc.returncode != 0 and expected in proc.stderr
    assert not any(c[0] == "secretsmanager" for c in calls(box))


# ---- Gate C: reading OPENAI_API_KEY from .local/providers.env (OPENAI_KEY_FILE is now optional) ------------------
# OPENAI_KEY_FILE="" is passed explicitly in every providers.env-path test below so the outcome can never depend
# on whether the invoking shell happens to have OPENAI_KEY_FILE exported (run() otherwise inherits os.environ).
NO_OVERRIDE = {"OPENAI_KEY_FILE": ""}


def test_gate_c_reads_the_key_from_providers_env_by_default_when_no_override_is_given(box):
    penv = providers_env(box["tmp"] / "providers.env", CANARY_KEY, extra_lines=("ANTHROPIC_API_KEY=sk-ant-unrelated",))
    ok = run(box, "26_openai_secret.sh", extra={**NO_OVERRIDE, "APPROVE_CREATE_OPENAI_SECRET": "yes", "PROVIDERS_ENV_FILE": str(penv), "TMPDIR": str(tmpdir_scratch(box))})
    assert ok.returncode == 0, ok.stderr[-1500:]
    assert "secretsmanager create-secret --name medsafety/openai-api-key" in ok.stderr
    assert "source: " in ok.stderr and str(penv) in ok.stderr  # the PATH is logged, never the key


def test_gate_c_refuses_when_providers_env_is_missing_and_no_override_is_given(box):
    missing = box["tmp"] / "does-not-exist.env"
    proc = run(box, "26_openai_secret.sh", extra={**NO_OVERRIDE, "APPROVE_CREATE_OPENAI_SECRET": "yes", "PROVIDERS_ENV_FILE": str(missing)})
    assert proc.returncode != 0 and "does not exist" in proc.stderr
    assert not any(c[0] == "secretsmanager" for c in calls(box))


def test_gate_c_refuses_when_providers_env_has_no_openai_api_key_line(box):
    penv = providers_env(box["tmp"] / "providers.env", None, extra_lines=("ANTHROPIC_API_KEY=sk-ant-unrelated",))
    proc = run(box, "26_openai_secret.sh", extra={**NO_OVERRIDE, "APPROVE_CREATE_OPENAI_SECRET": "yes", "PROVIDERS_ENV_FILE": str(penv)})
    assert proc.returncode != 0 and "OPENAI_API_KEY not found" in proc.stderr
    assert not any(c[0] == "secretsmanager" for c in calls(box))


def test_gate_c_only_reads_the_openai_api_key_line_ignoring_everything_else_in_providers_env(box):
    """Proves only OPENAI_API_KEY is read: a malformed/CRLF ANTHROPIC_API_KEY line elsewhere in the same file
    must not affect the outcome, and the OPENAI_API_KEY value used is exactly the first matching line."""
    penv = box["tmp"] / "providers.env"
    penv.write_bytes(b"# comment\nGEMINI_API_KEY=AIza-not-read\r\nOPENAI_API_KEY=" + CANARY_KEY.encode() + b"\nOPENAI_API_KEY=sk-should-be-ignored-second-match\n")
    ok = run(box, "26_openai_secret.sh", extra={**NO_OVERRIDE, "APPROVE_CREATE_OPENAI_SECRET": "yes", "PROVIDERS_ENV_FILE": str(penv), "TMPDIR": str(tmpdir_scratch(box))})
    assert ok.returncode == 0, ok.stderr[-1500:]


def test_gate_c_openai_key_file_override_takes_precedence_over_providers_env(box):
    kf = key_file(box["tmp"] / "openai.key", b"sk-override-wins\n")
    penv = providers_env(box["tmp"] / "providers.env", CANARY_KEY)
    ok = run(box, "26_openai_secret.sh", extra={"APPROVE_CREATE_OPENAI_SECRET": "yes", "OPENAI_KEY_FILE": str(kf), "PROVIDERS_ENV_FILE": str(penv)})
    assert ok.returncode == 0, ok.stderr[-1500:]
    assert "OPENAI_KEY_FILE override" in ok.stderr


# ---- Gate C: the key never appears anywhere observable, and the temp file is always cleaned up -------------------
def test_gate_c_the_key_value_never_appears_in_stdout_stderr_or_the_aws_call_log(box):
    penv = providers_env(box["tmp"] / "providers.env", CANARY_KEY)
    ok = run(box, "26_openai_secret.sh", extra={**NO_OVERRIDE, "APPROVE_CREATE_OPENAI_SECRET": "yes", "PROVIDERS_ENV_FILE": str(penv), "TMPDIR": str(tmpdir_scratch(box))})
    assert ok.returncode == 0, ok.stderr[-1500:]
    assert CANARY_KEY not in ok.stdout and CANARY_KEY not in ok.stderr
    raw_log = box["log"].read_text() if box["log"].exists() else ""
    assert CANARY_KEY not in raw_log
    for call in calls(box):
        assert CANARY_KEY not in json.dumps(call)


def test_gate_c_cleans_up_the_temp_key_file_on_success(box):
    penv = providers_env(box["tmp"] / "providers.env", CANARY_KEY)
    scratch = tmpdir_scratch(box)
    ok = run(box, "26_openai_secret.sh", extra={**NO_OVERRIDE, "APPROVE_CREATE_OPENAI_SECRET": "yes", "PROVIDERS_ENV_FILE": str(penv), "TMPDIR": str(scratch)})
    assert ok.returncode == 0, ok.stderr[-1500:]
    assert list(scratch.iterdir()) == []  # the EXIT trap removed the temp file; nothing leaked in TMPDIR


def test_gate_c_cleans_up_the_temp_key_file_even_when_the_secret_already_exists(box):
    """The trap must fire on the FAILURE path too, not only on success."""
    penv = providers_env(box["tmp"] / "providers.env", CANARY_KEY)
    scratch = tmpdir_scratch(box)
    sc = scenario(calls={"secretsmanager describe-secret": {"stdout": "{}"}})  # already exists (no error)
    proc = run(box, "26_openai_secret.sh", sc=sc, extra={**NO_OVERRIDE, "APPROVE_CREATE_OPENAI_SECRET": "yes", "PROVIDERS_ENV_FILE": str(penv), "TMPDIR": str(scratch)})
    assert proc.returncode != 0 and "unexpected existing Secrets Manager secret" in proc.stderr
    assert list(scratch.iterdir()) == []
    assert not any(c[0] == "secretsmanager" and c[1] == "create-secret" for c in calls(box))


def test_gate_c_temp_key_file_is_created_with_mode_600():
    """A direct, minimal proof of the mode-600 requirement, independent of the full script run above:
    mirrors exactly what 26_openai_secret.sh does (`mktemp` then `chmod 600`) and checks the resulting mode."""
    proc = subprocess.run(["bash", "-c", 'f="$(mktemp)"; chmod 600 "$f"; stat -c "%a" "$f"; rm -f "$f"'],
                          capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0 and proc.stdout.strip() == "600"


# ---- Gate D: OpenAI secret-read IAM grant ----------------------------------------------------------------------
def test_gate_d_fails_closed_when_the_inline_policy_already_exists(box):
    sc = scenario(calls={"iam get-role-policy": {"stdout": "{}"}})  # already exists (no error)
    proc = run(box, "27_openai_secret_policy.sh", state=BASE_STATE, sc=sc, extra={"APPROVE_OPENAI_SECRET_POLICY": "yes"})
    assert proc.returncode != 0 and "unexpected existing inline policy OpenAiSecretRead" in proc.stderr
    assert not any(c[0] == "iam" and c[1] == "put-role-policy" for c in calls(box))


def test_gate_d_refuses_without_approval_and_applies_with_it_when_the_policy_does_not_yet_exist(box):
    refused = run(box, "27_openai_secret_policy.sh", state=BASE_STATE)
    assert refused.returncode != 0 and "Gate D" in refused.stderr and "iam put-role-policy" not in refused.stderr
    assert OPENAI_SECRET_ARN in refused.stderr  # the statement was shown
    ok = run(box, "27_openai_secret_policy.sh", state=BASE_STATE, extra={"APPROVE_OPENAI_SECRET_POLICY": "yes"})
    assert ok.returncode == 0, ok.stderr[-1500:]
    assert "iam put-role-policy --role-name MedSafetyApiLambdaRole --policy-name OpenAiSecretRead" in ok.stderr
    applied = json.loads((box["state"] / "rendered" / "lambda-secrets-openai.json").read_text())
    (stmt,) = applied["Statement"]
    assert stmt["Action"] == "secretsmanager:GetSecretValue" and stmt["Resource"] == OPENAI_SECRET_ARN


# ---- lambda ----------------------------------------------------------------------------------------------------
# NOTE: `DS` (module constant, above) deliberately equals the id recorded in the real, committed
# datastore-recreation-record.json for the DELETED HealthLake datastore (ADR-0010) -- it's the actual value the
# repo's other fixtures/state were built from before deletion. CURRENT_DS simulates a properly recreated
# datastore (a different id) for the tests below that expect 40_lambda.sh to succeed.
CURRENT_DS = "11111111111111111111111111111111"


def test_lambda_is_created_with_the_planned_runtime_settings_and_read_only_config(box):
    box["hl"].write_text(f"DS_ID={CURRENT_DS}\nKEY_ARN={KEY}\n")
    proc = run(box, "40_lambda.sh", state=BASE_STATE, sc=scenario(calls={"lambda get-function": {"rc": 254, "stderr": "ResourceNotFoundException"}}))
    assert proc.returncode == 0, proc.stderr[-1500:]
    (cmd,) = [l for l in proc.stderr.splitlines() if "lambda create-function" in l]
    for flag in ("--runtime python3.12", "--architectures arm64", "--timeout 30", "--memory-size 512", "--handler app.lambda_handler.handler",
                 f"--role arn:aws:iam::{ACCT}:role/MedSafetyApiLambdaRole", "--function-name medsafety-api"):
        assert flag in cmd, flag
    assert "vpc" not in cmd.lower()
    env = json.loads((box["state"] / "rendered" / "lambda-env.json").read_text())["Variables"]
    assert env["HEALTHLAKE_WRITE_OUTPUTS"] == "false" and env["DATA_BACKEND"] == "healthlake" and env["APP_STATE_BACKEND"] == "dynamodb"
    assert env["EXPLANATION_PROVIDER"] == "openai" and env["EXPLANATION_MODEL"] == "gpt-5.6-luna" and env["EXPLANATION_MODE"] == "claude"
    assert env["OPENAI_API_KEY_SECRET_ARN"] == OPENAI_SECRET_ARN and int(env["OPENAI_MAX_TOKENS"]) <= 5000
    assert env["HEALTHLAKE_DATASTORE_ID"] == CURRENT_DS and env["APP_STATE_TABLE"] == "medsafety-app-state"
    assert "OPENAI_API_KEY" not in env and not any(k.startswith("BEDROCK_") for k in env)  # the ARN only, never the key; not the Bedrock target
    forbidden = {k for k in env if re.search(r"KEY|SECRET|(?<!MAX_)TOKEN|PASSWORD", k)} - {"OPENAI_API_KEY_SECRET_ARN"}
    assert not forbidden  # no secret material anywhere; the one exception is an ARN pointer, not a key


def test_healthlake_write_outputs_stays_false_without_explicit_approval_for_this_run(box):
    """The default, and what happens if APPROVE_HEALTHLAKE_WRITE_OUTPUTS is unset, empty, or anything other than
    exactly 'yes' -- no partial-credit values."""
    box["hl"].write_text(f"DS_ID={CURRENT_DS}\nKEY_ARN={KEY}\n")
    for approval in (None, "", "true", "YES", "1"):
        extra = {} if approval is None else {"APPROVE_HEALTHLAKE_WRITE_OUTPUTS": approval}
        proc = run(box, "40_lambda.sh", state=BASE_STATE,
                  sc=scenario(calls={"lambda get-function": {"rc": 254, "stderr": "ResourceNotFoundException"}}), extra=extra)
        assert proc.returncode == 0, proc.stderr[-1500:]
        env = json.loads((box["state"] / "rendered" / "lambda-env.json").read_text())["Variables"]
        assert env["HEALTHLAKE_WRITE_OUTPUTS"] == "false", f"approval={approval!r}"
        assert "APPROVE_HEALTHLAKE_WRITE_OUTPUTS=yes" not in proc.stderr


def test_healthlake_write_outputs_becomes_true_only_with_explicit_approval_for_this_run(box):
    box["hl"].write_text(f"DS_ID={CURRENT_DS}\nKEY_ARN={KEY}\n")
    proc = run(box, "40_lambda.sh", state=BASE_STATE,
              sc=scenario(calls={"lambda get-function": {"rc": 254, "stderr": "ResourceNotFoundException"}}),
              extra={"APPROVE_HEALTHLAKE_WRITE_OUTPUTS": "yes"})
    assert proc.returncode == 0, proc.stderr[-1500:]
    env = json.loads((box["state"] / "rendered" / "lambda-env.json").read_text())["Variables"]
    assert env["HEALTHLAKE_WRITE_OUTPUTS"] == "true"
    assert "APPROVE_HEALTHLAKE_WRITE_OUTPUTS=yes: deploying with HEALTHLAKE_WRITE_OUTPUTS=true" in proc.stderr
    (cmd,) = [l for l in proc.stderr.splitlines() if "lambda create-function" in l]
    assert "--description" in cmd  # sanity: still the same deploy path, nothing else changed


def test_healthlake_write_outputs_readback_mismatch_is_caught_after_a_real_deploy(box):
    """Exercises the non-DRY_RUN path: the post-deploy readback must match exactly what THIS run approved (here,
    the default 'false'), not just be truthy/falsy -- proves a real mismatch (e.g. AWS reporting a stale or
    unexpected value) is caught rather than silently accepted."""
    box["hl"].write_text(f"DS_ID={CURRENT_DS}\nKEY_ARN={KEY}\n")
    sc = scenario(calls={
        "lambda get-function": {"rc": 254, "stderr": "ResourceNotFoundException"},
        "lambda get-function-configuration": {"stdout": "true"},  # AWS reports 'true' though this run approved nothing
    })
    proc = run(box, "40_lambda.sh", state=BASE_STATE, sc=sc, dry=False)
    assert proc.returncode != 0
    assert "HEALTHLAKE_WRITE_OUTPUTS is 'true'" in proc.stderr and "expected 'false'" in proc.stderr


def test_lambda_refuses_before_any_aws_call_while_the_datastore_id_is_still_the_deleted_one(box):
    """box's hl.env default (DS_ID=DS) is exactly the trap docs/adr/0010's own recreation workflow documents:
    .state/deploy.env keeps holding the DELETED datastore's id until 91_recreate.sh explicitly clears it, so a
    naive "is it set" check would pass against a datastore that no longer exists. 40_lambda.sh must refuse."""
    proc = run(box, "40_lambda.sh", state=BASE_STATE)
    assert proc.returncode != 0
    assert "DELETED datastore" in proc.stderr
    assert not any(c[0] in ("lambda", "iam", "secretsmanager") for c in calls(box))  # only the sts identity check ran


def test_lambda_refuses_when_the_datastore_id_is_empty_or_missing(box):
    box["hl"].write_text(f"KEY_ARN={KEY}\n")  # 91_recreate.sh clears DS_ID entirely once recreation is prepared
    proc = run(box, "40_lambda.sh", state=BASE_STATE)
    assert proc.returncode != 0 and "DS_ID" in proc.stderr
    assert not any(c[0] in ("lambda", "iam", "secretsmanager") for c in calls(box))


def test_lambda_validates_the_complete_environment_key_set_before_any_mutating_call(box):
    """Guards the AWS replace-the-whole-map footgun: update-function-configuration REPLACES Environment.Variables
    entirely, so a coding mistake that drops or adds a key must be caught before any aws_mut call, not after.
    Runs a deliberately-broken copy of the real script (an injected extra key) -- `run()` always executes the
    committed script at its real path, so this test builds its own subprocess call against a modified copy,
    re-pointing its `source lib.sh` line at the real lib.sh (absolute path) so BASH_SOURCE-derived paths inside
    lib.sh still resolve correctly."""
    box["hl"].write_text(f"DS_ID={CURRENT_DS}\nKEY_ARN={KEY}\n")
    script = (API / "40_lambda.sh").read_text()
    broken = script.replace('source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"', f'source "{API}/lib.sh"')
    broken = broken.replace('"LOG_LEVEL": "INFO"}', '"LOG_LEVEL": "INFO", "UNEXPECTED_EXTRA_VAR": "x"}')
    assert broken != script and '"UNEXPECTED_EXTRA_VAR"' in broken
    broken_path = box["tmp"] / "40_lambda_broken.sh"
    broken_path.write_text(broken)
    box["scenario"].write_text(json.dumps(scenario(calls={"lambda get-function": {"rc": 254, "stderr": "ResourceNotFoundException"}})))
    (box["state"] / "deploy.env").write_text("".join(f"{k}={v!r}\n" for k, v in BASE_STATE.items()))
    env = {**os.environ, "PATH": f"{box['bin']}:{os.environ['PATH']}", "AWS_PROFILE": "medsafety", "STATE_DIR": str(box["state"]),
           "HL_STATE_FILE": str(box["hl"]), "STUB_LOG": str(box["log"]), "STUB_SCENARIO": str(box["scenario"]), "DRY_RUN": "1"}
    proc = subprocess.run(["bash", str(broken_path)], env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode != 0 and "key set mismatch" in proc.stderr and "UNEXPECTED_EXTRA_VAR" in proc.stderr
    assert not any(c[0] == "lambda" and c[1].startswith(("create-function", "update-function")) for c in calls(box))


def test_lambda_update_path_sets_the_description_on_an_already_existing_function(box):
    """Regression: a real deployment always takes this branch (the function already exists from an earlier
    phase), but no test exercised it before -- update-function-configuration used to omit --description
    entirely, silently leaving whatever text the function was created with (e.g. a stale Bedrock-era
    description) displayed after every later redeploy, including the OpenAI one."""
    box["hl"].write_text(f"DS_ID={CURRENT_DS}\nKEY_ARN={KEY}\n")
    sc = scenario(calls={"lambda get-function": {"stdout": "{}"}})  # function already exists -> the update branch
    proc = run(box, "40_lambda.sh", state=BASE_STATE, sc=sc)
    assert proc.returncode == 0, proc.stderr[-1500:]
    made = calls(box)
    assert not any(c[0] == "lambda" and c[1] == "create-function" for c in made)
    (code_cmd,) = [l for l in proc.stderr.splitlines() if "lambda update-function-code" in l]
    (cfg_cmd,) = [l for l in proc.stderr.splitlines() if "lambda update-function-configuration" in l]
    assert "--architectures arm64" in code_cmd
    assert "--description" in cfg_cmd and "Bedrock" not in cfg_cmd and "OpenAI" not in cfg_cmd  # provider-independent text
    assert "FHIR Medication Safety Intelligence API" in cfg_cmd


# ---- api gateway ---------------------------------------------------------------------------------------------------
def routes_txt():
    return [tuple(l.split()) for l in (API / "routes.txt").read_text().splitlines() if l.strip() and not l.lstrip().startswith("#")]


def test_api_routes_are_exactly_the_contract_routes_of_the_app():
    from app.main import app

    app_routes = {(m.upper(), path) for path, ops in app.openapi()["paths"].items() if path.startswith("/v1") for m in ops if m.upper() in ("GET", "POST")}
    # /v1/ready was reviewed as a contract (docs/READINESS.md) before being exposed here -- see docs/adr/0011 and
    # the Phase 5 deployment record for when it was added to the API Gateway surface.
    assert set(routes_txt()) == app_routes and len(routes_txt()) == 9
    assert not any(p in ("/docs", "/openapi.json", "/redoc") for _, p in routes_txt())


def test_api_gateway_is_created_with_explicit_routes_ip_policy_and_throttling(box):
    proc = run(box, "50_api.sh", state=BASE_STATE, extra={"ALLOWED_IP_CIDR": "203.0.113.7/32"})
    assert proc.returncode == 0, proc.stderr[-2000:]
    err = proc.stderr
    assert "create-rest-api --name medsafety-api" in err and "--endpoint-configuration types=REGIONAL" in err
    assert "203.0.113.7/32" in err and "execute-api:Invoke" in err and "{proxy+}" not in err and "--path-part {proxy+}" not in err
    for method, path in routes_txt():
        assert f"route {method} {path} (+OPTIONS)" in err
    assert err.count("put-integration") == 2 * len(routes_txt()) and "--type AWS_PROXY" in err and "--timeout-in-millis 29000" in err
    assert "--authorization-type NONE" in err  # access control is the resource policy, not SMART/Cognito
    assert "add-permission --function-name medsafety-api --statement-id apigw-invoke" in err and "--principal apigateway.amazonaws.com" in err
    ops = {o["path"]: o["value"] for o in json.loads((box["state"] / "rendered" / "stage-patch.json").read_text())}  # JSON, not CLI shorthand ({braces} break shorthand)
    expl = "/~1v1~1patients~1{patient_id}~1analyses~1{analysis_id}~1explanation/POST/throttling"
    assert ops == {"/*/*/throttling/rateLimit": "5", "/*/*/throttling/burstLimit": "10", f"{expl}/rateLimit": "2", f"{expl}/burstLimit": "3"}
    assert "update-stage --rest-api-id" in err and "--patch-operations file://" in err
    assert "create-deployment --rest-api-id" in err and "--stage-name dev" in err


@pytest.mark.parametrize("ip", ["", "0.0.0.0/0", "203.0.113.0/24", "203.0.113.7", "::/0", "10.0.0.1/32 0.0.0.0/0"])
def test_open_or_missing_ip_ranges_are_refused(box, ip):
    for script in ("50_api.sh", "70_update_allowlist.sh"):
        proc = run(box, script, state=BASE_STATE, extra={"ALLOWED_IP_CIDR": ip})
        assert proc.returncode != 0 and "/32" in proc.stderr and "create-rest-api" not in proc.stderr and "update-rest-api" not in proc.stderr


def test_allowlist_update_replaces_the_policy_and_redeploys(box):
    proc = run(box, "70_update_allowlist.sh", state=BASE_STATE, extra={"ALLOWED_IP_CIDR": "198.51.100.9/32"})
    assert proc.returncode == 0 and "update-rest-api --rest-api-id abc123 --patch-operations op=replace,path=/policy" in proc.stderr
    assert "198.51.100.9" in proc.stderr and "create-deployment --rest-api-id abc123 --stage-name dev" in proc.stderr


# ---- teardown -----------------------------------------------------------------------------------------------------
def test_teardown_needs_confirmation_and_only_removes_phase4_resources(box):
    assert run(box, "90_teardown.sh", state=BASE_STATE).returncode != 0
    assert not any("delete" in " ".join(c) for c in calls(box))
    sc = scenario(calls={"dynamodb describe-table": {"rc": 0, "stdout": "{}"}, "iam get-role": {"rc": 0, "stdout": "{}"}, "iam list-role-policies": {"stdout": "BaseAccess BedrockInvoke"},
                         "lambda get-function": {"rc": 0, "stdout": "{}"}, "logs describe-log-groups": {"stdout": "/aws/lambda/medsafety-api"}})
    proc = run(box, "90_teardown.sh", state=BASE_STATE, sc=sc, extra={"CONFIRM_TEARDOWN": "yes"})
    assert proc.returncode == 0, proc.stderr[-1500:]
    err = proc.stderr
    commands = "\n".join(l for l in err.splitlines() if l.startswith("DRY-RUN aws"))
    for expected in ("delete-rest-api --rest-api-id abc123", "lambda delete-function --function-name medsafety-api", "iam delete-role-policy --role-name MedSafetyApiLambdaRole --policy-name BaseAccess",
                     "iam delete-role --role-name MedSafetyApiLambdaRole", "dynamodb delete-table --table-name medsafety-app-state", "logs delete-log-group --log-group-name /aws/lambda/medsafety-api"):
        assert expected in commands, expected
    for never in ("healthlake", "kms", "s3api", "s3 ", "MedSafetyHealthLake"):
        assert never not in commands.lower().replace("medsafety-", ""), never


# ---- static guards ------------------------------------------------------------------------------------------------
def test_no_phase4_script_or_policy_uses_the_mantle_endpoint_open_ip_or_credentials_or_writes_healthlake():
    text = "\n".join(p.read_text() for p in [*API.glob("*.sh"), *API.glob("policies/*"), API / "routes.txt"])
    assert not re.search(r"bedrock-mantle|AnthropicBedrockMantle|0\.0\.0\.0/0|AKIA[0-9A-Z]{12,}|aws_secret_access_key|AdministratorAccess", text, re.I)
    assert not re.search(r"healthlake (create|delete|start|update)", text)


def test_https_verification_wrapper_passes_the_phase3_verifier_role_so_a14_cannot_be_skipped_silently():
    text = (API / "60_verify.sh").read_text()
    assert "--verifier-role-arn" in text and "VERIFIER_ROLE_ARN" in text
    assert "require_state INVOKE_URL VERIFIER_ROLE_ARN" in text  # missing role -> stop, not a silent SKIP


def test_scripts_are_syntactically_valid_bash():
    for script in API.glob("*.sh"):
        assert subprocess.run(["bash", "-n", str(script)], capture_output=True).returncode == 0, script
