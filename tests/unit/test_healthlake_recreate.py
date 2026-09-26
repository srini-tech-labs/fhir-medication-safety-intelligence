"""91_recreate.sh (HealthLake recreation preflight) against a stub `aws`, offline. It must never call a mutating
API; it only reads, validates the recreation record against live-simulated state, and rewrites the local state file."""
from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from app.config import REPO_ROOT

HL = REPO_ROOT / "infrastructure" / "aws" / "healthlake"
ACCT = "123456789012"
OLD_DS = "0123456789abcdef0123456789abcdef"
NEW_KEY_ARN = f"arn:aws:kms:us-east-1:{ACCT}:key/11111111-2222-3333-4444-555555555555"
IMPORT_BUCKET = f"medsafety-hl-import-{ACCT}-us-east-1"
RESULTS_BUCKET = f"medsafety-hl-results-{ACCT}-us-east-1"

RECORD = {
    "datastore": {"id": OLD_DS, "fhirVersion": "R4", "authorizationStrategy": "AWS_AUTH",
                  "createClientToken": "medsafety-phase3-ds-001 (a NEW token is needed to recreate)"},
    "importJob": {"dataAccessRoleArn": f"arn:aws:iam::{ACCT}:role/MedSafetyHealthLakeImportRole",
                  "clientToken": "medsafety-phase0-v1-import-001 (a NEW token is needed to recreate)"},
    "kms": {"keyArn": NEW_KEY_ARN, "alias": "alias/medsafety-healthlake-cmk"},
    "s3ObjectsAtRecordTime": {IMPORT_BUCKET: [{"key": f"phase0-v1/f{i}.ndjson"} for i in range(6)],
                              RESULTS_BUCKET: [{"key": f"phase0-v1/r{i}"} for i in range(8)]},
}

# STUB_* env vars let each test simulate one live condition without touching real AWS.
STUB = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["STUB_LOG"], "a") as f:
    f.write(json.dumps(args) + "\n")
q = " ".join(args)
def out(s=""): print(s); sys.exit(0)
if args[:1] == ["--version"]: out("aws-cli/2.36.49 stub")
if args[:2] == ["sts", "get-caller-identity"]:
    out(os.environ["STUB_ACCOUNT"] if "Account" in q else os.environ["STUB_ARN"])
if args[:2] == ["healthlake", "list-fhir-datastores"]:
    out(os.environ.get("STUB_EXISTING_DS", ""))
if args[:2] == ["kms", "describe-key"]:
    out(os.environ.get("STUB_KEY_STATE", "Enabled\tTrue"))
if args[:2] == ["kms", "list-aliases"]:
    out(os.environ.get("STUB_ALIAS", "alias/medsafety-healthlake-cmk") if os.environ.get("STUB_ALIAS_OK", "1") == "1" else "")
if args[:2] == ["iam", "get-role"]:
    sys.exit(0 if os.environ.get("STUB_ROLES_OK", "1") == "1" else 254)
if args[:2] == ["s3api", "head-bucket"]:
    sys.exit(0 if os.environ.get("STUB_BUCKETS_OK", "1") == "1" else 254)
if args[:2] == ["s3api", "list-objects-v2"]:
    bucket = args[args.index("--bucket") + 1]
    counts = json.loads(os.environ.get("STUB_BUCKET_COUNTS", "{}"))
    out(str(counts.get(bucket, -1)))
out("")
'''


@pytest.fixture()
def sandbox(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "aws"
    stub.write_text(STUB)
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    state = tmp_path / "state"
    state.mkdir()
    record = tmp_path / "record.json"
    record.write_text(json.dumps(RECORD))
    return {"bin": bindir, "state": state, "log": tmp_path / "aws.log", "record": record}


def run(sandbox, *, state: dict | None = None, extra_env: dict | None = None, dry: bool = False):
    if state is not None:
        (sandbox["state"] / "deploy.env").write_text("".join(f"{k}={v}\n" for k, v in state.items()))
    env = {**os.environ, "PATH": f"{sandbox['bin']}:{os.environ['PATH']}", "STUB_LOG": str(sandbox["log"]),
           "STUB_ACCOUNT": ACCT, "STUB_ARN": f"arn:aws:iam::{ACCT}:user/deployer",
           "AWS_PROFILE": "medsafety-test", "STATE_DIR": str(sandbox["state"]), "RECREATE_RECORD": str(sandbox["record"]),
           "CONFIRM_RECREATE": "yes", "RECREATE_DS_CLIENT_TOKEN": "medsafety-phase3-ds-002",
           "RECREATE_JOB_CLIENT_TOKEN": "medsafety-phase0-v1-import-002",
           "STUB_BUCKET_COUNTS": json.dumps({IMPORT_BUCKET: 6, RESULTS_BUCKET: 8}),
           **(extra_env or {})}
    if dry:
        env["DRY_RUN"] = "1"
    return subprocess.run(["bash", str(HL / "91_recreate.sh")], env=env, capture_output=True, text=True, timeout=60)


def calls(sandbox) -> list[list[str]]:
    return [json.loads(l) for l in sandbox["log"].read_text().splitlines()] if sandbox["log"].exists() else []


def test_requires_explicit_confirmation():
    proc = subprocess.run(["bash", str(HL / "91_recreate.sh")],
                          env={**os.environ, "AWS_PROFILE": "medsafety-test"}, capture_output=True, text=True)
    assert proc.returncode != 0 and "CONFIRM_RECREATE=yes" in proc.stderr


def test_requires_new_client_tokens_distinct_from_the_recorded_ones(sandbox):
    proc = run(sandbox, extra_env={"RECREATE_DS_CLIENT_TOKEN": "medsafety-phase3-ds-001"})  # the OLD, recorded token
    assert proc.returncode != 0 and "reuses the token bound to the deleted datastore" in proc.stderr
    proc = run(sandbox, extra_env={"RECREATE_JOB_CLIENT_TOKEN": "medsafety-phase0-v1-import-001"})
    assert proc.returncode != 0 and "reuses the token bound to the deleted datastore's import job" in proc.stderr


def test_refuses_to_proceed_while_a_live_non_deleted_datastore_exists(sandbox):
    proc = run(sandbox, extra_env={"STUB_EXISTING_DS": f"{OLD_DS}\tACTIVE"})
    assert proc.returncode != 0 and "already exists and is not DELETED" in proc.stderr


def test_refuses_if_the_cmk_is_not_enabled(sandbox):
    proc = run(sandbox, extra_env={"STUB_KEY_STATE": "PendingDeletion\tFalse"})
    assert proc.returncode != 0 and "not Enabled" in proc.stderr


def test_refuses_if_the_alias_no_longer_points_at_the_recorded_key(sandbox):
    proc = run(sandbox, extra_env={"STUB_ALIAS_OK": "0"})
    assert proc.returncode != 0 and "no longer points at" in proc.stderr


def test_refuses_if_an_iam_role_the_recreation_depends_on_is_missing(sandbox):
    proc = run(sandbox, extra_env={"STUB_ROLES_OK": "0"})
    assert proc.returncode != 0 and "is missing" in proc.stderr


def test_refuses_if_a_bucket_object_count_no_longer_matches_the_record(sandbox):
    proc = run(sandbox, extra_env={"STUB_BUCKET_COUNTS": json.dumps({IMPORT_BUCKET: 5, RESULTS_BUCKET: 8})})
    assert proc.returncode != 0 and f"bucket {IMPORT_BUCKET}" in proc.stderr and "investigate" in proc.stderr


def test_refuses_if_a_bucket_is_missing(sandbox):
    proc = run(sandbox, extra_env={"STUB_BUCKETS_OK": "0"})
    assert proc.returncode != 0 and "no longer exists" in proc.stderr


def test_never_calls_a_mutating_api(sandbox):
    proc = run(sandbox, state={"DS_ID": OLD_DS, "JOB_ID": "old-job-id"})
    assert proc.returncode == 0, proc.stderr[-1500:]
    used = {c[0] for c in calls(sandbox)}
    assert used <= {"--version", "sts", "healthlake", "kms", "iam", "s3api"}
    for c in calls(sandbox):
        joined = " ".join(c)
        assert "create" not in joined and "put-role-policy" not in joined and "start-fhir-import-job" not in joined \
            and "delete" not in joined and "update-" not in joined


def test_clears_only_the_datastore_specific_stale_state_and_backs_it_up(sandbox):
    state = {"DS_ID": OLD_DS, "DS_ARN": f"arn:aws:healthlake:us-east-1:{ACCT}:datastore/fhir/{OLD_DS}",
             "DS_ENDPOINT": "https://old", "DS_CREATED_AT": "2026-09-19T06:00:56Z", "JOB_ID": "old-job-id",
             "KEY_ARN": NEW_KEY_ARN, "IMPORT_ROLE_ARN": f"arn:aws:iam::{ACCT}:role/MedSafetyHealthLakeImportRole"}
    proc = run(sandbox, state=state)
    assert proc.returncode == 0, proc.stderr[-1500:]
    remaining = (sandbox["state"] / "deploy.env").read_text()
    for k in ("DS_ID=", "DS_ARN=", "DS_ENDPOINT=", "DS_CREATED_AT=", "JOB_ID="):
        assert k not in remaining, f"{k} should have been cleared"
    assert "KEY_ARN=" in remaining and "IMPORT_ROLE_ARN=" in remaining  # unrelated state is preserved
    backups = list(sandbox["state"].glob("deploy.env.pre-recreate-*"))
    assert len(backups) == 1
    assert f"DS_ID={OLD_DS}" in backups[0].read_text()  # the stale id is recoverable from the backup


def test_dry_run_prepares_nothing_and_leaves_state_untouched(sandbox):
    state = {"DS_ID": OLD_DS, "JOB_ID": "old-job-id"}
    proc = run(sandbox, state=state, dry=True)
    assert proc.returncode == 0, proc.stderr[-1500:]
    assert (sandbox["state"] / "deploy.env").read_text() == "".join(f"{k}={v}\n" for k, v in state.items())
    assert not list(sandbox["state"].glob("deploy.env.pre-recreate-*"))
    assert "DRY_RUN: would" in proc.stderr


def test_prints_the_ordered_recreation_plan_with_the_new_tokens(sandbox):
    proc = run(sandbox)
    assert proc.returncode == 0, proc.stderr[-1500:]
    out = proc.stdout
    for step in ("20_datastore.sh", "30_iam.sh", "50_import.sh", "healthlake_verify.py", "--checks V0,V1,V2,V3,V4,V5",
                 "infrastructure/aws/api/20_role.sh", "infrastructure/aws/api/40_lambda.sh", "api_verify.py", "60_verify.sh"):
        assert step in out, step
    assert "medsafety-phase3-ds-002" in out and "medsafety-phase0-v1-import-002" in out
    assert out.index("20_datastore.sh") < out.index("30_iam.sh") < out.index("50_import.sh") \
        < out.index("--checks V0,V1,V2,V3,V4,V5") < out.index("60_verify.sh")


def test_missing_record_file_stops_before_any_aws_call(sandbox):
    proc = run(sandbox, extra_env={"RECREATE_RECORD": str(sandbox["state"] / "nope.json")})
    assert proc.returncode != 0 and "recreation record not found" in proc.stderr
    assert calls(sandbox) == []
