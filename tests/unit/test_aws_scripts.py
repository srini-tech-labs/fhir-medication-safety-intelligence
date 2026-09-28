"""Deployment scripts run against a stub `aws` executable (offline). Proves ordering guards, read-only preflight, exact flags,
and that nothing weakens validation. Mutating calls are skipped by DRY_RUN=1 and echoed as `DRY-RUN aws ...`."""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

from app.config import REPO_ROOT

HL = REPO_ROOT / "infrastructure" / "aws" / "healthlake"
ACCT = "123456789012"
DS = "0123456789abcdef0123456789abcdef"
KEY_ARN = f"arn:aws:kms:us-east-1:{ACCT}:key/11111111-2222-3333-4444-555555555555"
SSO_ROLE = f"arn:aws:iam::{ACCT}:role/aws-reserved/sso.amazonaws.com/AWSReservedSSO_Deployer_0123abcd"

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
if args[:2] == ["iam", "get-role"]:
    if "Role.Arn" in q: out(os.environ["STUB_ROLE_ARN"])
    sys.exit(254)  # role does not exist yet
if args[:2] == ["s3api", "head-bucket"]:
    sys.stderr.write("An error occurred (404) when calling the HeadBucket operation: Not Found\n"); sys.exit(254)
if args[:2] == ["healthlake", "list-fhir-datastores"] or args[:2] == ["healthlake", "list-fhir-import-jobs"] \
        or args[:2] == ["kms", "list-aliases"] or args[:2] == ["cloudtrail", "describe-trails"]:
    out("")
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
    return {"bin": bindir, "state": state, "log": tmp_path / "aws.log"}


def run(sandbox, script: str, *, arn: str | None = None, dry: bool = True, state: dict | None = None, extra_env: dict | None = None):
    if state is not None:
        (sandbox["state"] / "deploy.env").write_text("".join(f"{k}={v}\n" for k, v in state.items()))
    env = {**os.environ, "PATH": f"{sandbox['bin']}:{os.environ['PATH']}", "STUB_LOG": str(sandbox["log"]),
           "STUB_ACCOUNT": ACCT, "STUB_ARN": arn or f"arn:aws:iam::{ACCT}:user/deployer", "STUB_ROLE_ARN": SSO_ROLE,
           "AWS_PROFILE": "medsafety-test", "STATE_DIR": str(sandbox["state"]), **(extra_env or {})}
    if dry:
        env["DRY_RUN"] = "1"
    return subprocess.run(["bash", str(HL / script)], env=env, capture_output=True, text=True, timeout=120)


def calls(sandbox) -> list[list[str]]:
    return [json.loads(l) for l in sandbox["log"].read_text().splitlines()] if sandbox["log"].exists() else []


STATE = {"KEY_ARN": KEY_ARN, "KEY_ID": "11111111-2222-3333-4444-555555555555", "DS_ID": DS,
         "IMPORT_BUCKET": f"medsafety-hl-import-{ACCT}-us-east-1", "RESULTS_BUCKET": f"medsafety-hl-results-{ACCT}-us-east-1",
         "IMPORT_ROLE_ARN": f"arn:aws:iam::{ACCT}:role/MedSafetyHealthLakeImportRole"}


def test_profile_is_required():
    proc = subprocess.run(["bash", str(HL / "00_preflight.sh")], env={k: v for k, v in os.environ.items() if k != "AWS_PROFILE"},
                          capture_output=True, text=True)
    assert proc.returncode != 0 and "AWS_PROFILE" in proc.stderr


def test_preflight_is_strictly_read_only_and_passes_the_package_check(sandbox):
    proc = run(sandbox, "00_preflight.sh", dry=False)  # NOT dry-run: prove it never mutates even when allowed to
    assert proc.returncode == 0, proc.stderr[-1500:]
    used = {" ".join(c[:2]) for c in calls(sandbox)}
    assert used <= {"sts get-caller-identity", "healthlake list-fhir-datastores", "s3api head-bucket", "kms list-aliases",
                    "cloudtrail describe-trails", "--version"}
    assert "package checksums OK" in proc.stderr and "PREFLIGHT OK" in proc.stderr
    assert f"medsafety-hl-import-{ACCT}-us-east-1" in proc.stderr


def test_root_identity_is_refused(sandbox):
    proc = run(sandbox, "00_preflight.sh", arn=f"arn:aws:iam::{ACCT}:root", dry=False)
    assert proc.returncode != 0 and "root" in proc.stderr


def test_assumed_role_session_arn_is_resolved_to_the_underlying_role_principal(sandbox):
    proc = run(sandbox, "30_iam.sh", arn=f"arn:aws:sts::{ACCT}:assumed-role/AWSReservedSSO_Deployer_0123abcd/someone@example.com", state=STATE)
    assert proc.returncode == 0, proc.stderr[-1500:]
    assert any(c[:2] == ["iam", "get-role"] and "Role.Arn" in " ".join(c) for c in calls(sandbox))
    for role in ("MedSafetyHealthLakeAppRole", "MedSafetyHealthLakeVerifierRole"):
        trust = json.loads((sandbox["state"] / "rendered" / f"{role}.trust.json").read_text())
        assert trust["Statement"][0]["Principal"]["AWS"] == SSO_ROLE  # long-term role, with its path; not the STS session
    assert "assumed-role" not in (sandbox["state"] / "rendered" / "MedSafetyHealthLakeAppRole.trust.json").read_text()


def test_explicit_principal_override_is_validated(sandbox):
    proc = run(sandbox, "30_iam.sh", arn=f"arn:aws:sts::{ACCT}:assumed-role/X/s", state=STATE,
               extra_env={"PRINCIPAL_ARN": f"arn:aws:sts::{ACCT}:assumed-role/X/s"})
    assert proc.returncode != 0 and "PRINCIPAL_ARN" in proc.stderr


def test_iam_step_renders_the_approved_import_trust_and_requires_the_datastore_first(sandbox):
    proc = run(sandbox, "30_iam.sh", state={k: v for k, v in STATE.items() if k != "DS_ID"})
    assert proc.returncode != 0 and "missing DS_ID" in proc.stderr  # ordering guard: datastore before the import role
    proc = run(sandbox, "30_iam.sh", state=STATE)
    assert proc.returncode == 0, proc.stderr[-1500:]
    trust = json.loads((sandbox["state"] / "rendered" / "MedSafetyHealthLakeImportRole.trust.json").read_text())
    assert trust["Statement"][0]["Condition"]["ArnEquals"]["aws:SourceArn"] == f"arn:aws:healthlake:us-east-1:{ACCT}:datastore/fhir/{DS}"
    assert "DRY-RUN aws iam create-role --role-name MedSafetyHealthLakeImportRole" in proc.stderr


def test_datastore_is_created_with_cmk_and_default_sigv4_auth(sandbox):
    proc = run(sandbox, "20_datastore.sh", state=STATE)
    assert proc.returncode == 0, proc.stderr[-1500:]
    (cmd,) = [l for l in proc.stderr.splitlines() if "create-fhir-datastore" in l]
    assert "--datastore-type-version R4" in cmd and "CUSTOMER_MANAGED_KMS_KEY" in cmd and KEY_ARN in cmd
    assert "--datastore-name medsafety-fhir-r4" in cmd and "--client-token medsafety-phase3-ds-001" in cmd
    assert "identity-provider" not in cmd.lower() and "Project,Value=medsafety-intelligence" in cmd  # no SMART => AWS_AUTH
    assert "--analytics-configuration Status=PAUSED" in cmd  # user decision; DISABLED is rejected at creation (ENABLED|PAUSED only)
    assert "Status=DISABLED" not in cmd
    assert "nlp" not in cmd.lower() and "preload" not in cmd.lower()


def test_import_is_strict_idempotent_and_pinned_to_the_planned_paths(sandbox):
    proc = run(sandbox, "50_import.sh", state=STATE)
    assert proc.returncode == 0, proc.stderr[-1500:]
    (cmd,) = [l for l in proc.stderr.splitlines() if "start-fhir-import-job" in l]
    assert "--validation-level strict" in cmd and "--client-token medsafety-phase0-v1-import-001" in cmd
    assert f"S3Uri=s3://medsafety-hl-import-{ACCT}-us-east-1/phase0-v1/" in cmd
    assert f"S3Uri=s3://medsafety-hl-results-{ACCT}-us-east-1/phase0-v1/,KmsKeyId={KEY_ARN}" in cmd
    assert f"--data-access-role-arn arn:aws:iam::{ACCT}:role/MedSafetyHealthLakeImportRole" in cmd


def test_import_never_trusts_a_job_id_left_in_state_by_a_previous_deleted_datastore(sandbox):
    """Regression: a JOB_ID left in .state/deploy.env from a datastore that was since deleted and recreated must not
    be reused -- the new datastore never has a job by that id. The fix always re-resolves the job id live, scoped to
    the CURRENT DS_ID, before ever using it."""
    proc = run(sandbox, "50_import.sh", state={**STATE, "JOB_ID": "stale-job-from-a-deleted-datastore"})
    assert proc.returncode == 0, proc.stderr[-1500:]
    made = calls(sandbox)
    assert ["healthlake", "list-fhir-import-jobs"] == [c[:2] for c in made if c[:2] == ["healthlake", "list-fhir-import-jobs"]][0:1][0]
    assert not any(c[:2] == ["healthlake", "describe-fhir-import-job"] and "stale-job-from-a-deleted-datastore" in c for c in made)
    (cmd,) = [l for l in proc.stderr.splitlines() if "start-fhir-import-job" in l]  # fresh job started, not the stale id reused
    assert "--client-token medsafety-phase0-v1-import-001" in cmd


def _extract_manifest_pick_heredoc() -> str:
    """Pulls the exact python heredoc 50_import.sh embeds for manifest-count verification, so this test always
    exercises the real, current script logic -- never a hand-copied reimplementation that could drift out of sync."""
    text = (HL / "50_import.sh").read_text()
    start = text.index("python3 - \"$STATE_DIR/import-manifest.json\"")
    heredoc_marker_end = text.index("<<'PY'", start) + len("<<'PY'")
    body_start = text.index("\n", heredoc_marker_end) + 1  # skip past the trailing `|| die "..."` on the same line
    body_end = text.index("\nPY", body_start)
    return text[body_start:body_end]


REAL_MANIFEST_SHAPE = {  # exact key order HealthLake's own Manifest.json uses (captured from a real import)
    "inputDataConfig": {"s3Uri": "s3://bucket/phase0-v1/"},
    "outputDataConfig": {"s3Uri": "s3://results/phase0-v1/job/", "encryptionKeyID": "arn:aws:kms:..."},
    "successOutput": {"successOutputS3Uri": "s3://results/phase0-v1/job/SUCCESS/"},
    "failureOutput": {"failureOutputS3Uri": "s3://results/phase0-v1/job/FAILURE/"},
    "numberOfScannedFiles": 6,
    "numberOfFilesImported": 6,
    "numberOfFilesReadWithSuccess": 6,
    "numberOfFilesReadWithCustomerError": 0,
    "numberOfFilesReadWithServerError": 0,
    "sizeOfScannedFilesInMB": 0.032575,
    "sizeOfDataImportedSuccessfullyInMB": 0.032522,
    "numberOfResourcesScanned": 53,
    "numberOfResourcesImportedSuccessfully": 53,
    "numberOfResourcesWithCustomerError": 0,
    "numberOfResourcesWithServerError": 0,
}


def test_manifest_pick_reads_resource_counts_not_file_counts_or_the_success_output_uri_dict(tmp_path):
    """Regression: a live recreation run crashed here with `TypeError: int() argument ... not 'dict'` because the
    original pick() matched ANY key containing a needle -- `successOutput` (a dict holding an S3 URI) sorts before
    `numberOfResourcesImportedSuccessfully` in HealthLake's own key order, and `numberOfScannedFiles` (a FILE count)
    sorts before `numberOfResourcesScanned` for the "scanned" needle. Both are wrong matches for a RESOURCE count."""
    manifest = tmp_path / "import-manifest.json"
    manifest.write_text(json.dumps(REAL_MANIFEST_SHAPE))
    proc = subprocess.run(["python3", "-c", _extract_manifest_pick_heredoc(), str(manifest), "53"],
                          capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0, proc.stderr
    assert "(53, 53, 0, 0)" in proc.stdout


def test_manifest_pick_still_fails_closed_on_a_real_customer_error_count(tmp_path):
    bad = {**REAL_MANIFEST_SHAPE, "numberOfResourcesWithCustomerError": 2, "numberOfResourcesImportedSuccessfully": 51}
    manifest = tmp_path / "import-manifest.json"
    manifest.write_text(json.dumps(bad))
    proc = subprocess.run(["python3", "-c", _extract_manifest_pick_heredoc(), str(manifest), "53"],
                          capture_output=True, text=True, timeout=10)
    assert proc.returncode == 1  # correctly reports a mismatch, does not silently pass


def test_upload_sends_six_frozen_files_with_sha256_and_kms(sandbox):
    proc = run(sandbox, "40_stage_upload.sh", state=STATE)
    assert proc.returncode == 0, proc.stderr[-1500:]
    puts = [l for l in proc.stderr.splitlines() if "s3api put-object" in l]
    assert [re.search(r"phase0-v1/(\w+)\.ndjson", l).group(1) for l in puts] == \
        ["Patient", "Encounter", "MedicationRequest", "Observation", "DocumentReference", "Binary"]
    for l in puts:
        assert "--checksum-algorithm SHA256" in l and "--server-side-encryption aws:kms" in l and f"--ssekms-key-id {KEY_ARN}" in l
        assert "data/phase0_v1_0/fhir/bulk/" in l  # the frozen files themselves, not a transformed copy


def test_foundation_creates_the_hardened_key_and_buckets(sandbox):
    proc = run(sandbox, "10_foundation.sh")
    assert proc.returncode == 0, proc.stderr[-1500:]
    err = proc.stderr
    assert "kms create-key" in err and "--key-spec SYMMETRIC_DEFAULT" in err and "kms enable-key-rotation" in err
    assert "kms create-alias --alias-name alias/medsafety-healthlake-cmk" in err
    for b in (f"medsafety-hl-import-{ACCT}-us-east-1", f"medsafety-hl-results-{ACCT}-us-east-1"):
        for verb in ("create-bucket", "put-public-access-block", "put-bucket-ownership-controls", "put-bucket-versioning",
                     "put-bucket-encryption", "put-bucket-policy", "put-bucket-lifecycle-configuration", "put-bucket-tagging"):
            assert re.search(rf"s3api {verb} --bucket {b}\b", err), (verb, b)
    assert "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true" in err


def test_teardown_needs_explicit_confirmation(sandbox):
    proc = run(sandbox, "90_teardown.sh", state=STATE)
    assert proc.returncode != 0 and "CONFIRM_TEARDOWN=yes" in proc.stderr
    assert not any(c[0] in ("healthlake",) and "delete" in " ".join(c) for c in calls(sandbox))


def test_teardown_dry_run_lists_every_deletion_in_order(sandbox):
    proc = run(sandbox, "90_teardown.sh", state=STATE, extra_env={"CONFIRM_TEARDOWN": "yes"})
    assert proc.returncode == 0, proc.stderr[-1500:]
    err = proc.stderr
    assert err.index("delete-fhir-datastore") < err.index("kms schedule-key-deletion")
    assert "--pending-window-in-days 7" in err
    # the Glue resource-link database HealthLake creates for analytics is removed, by exact name only
    assert f"glue delete-database --name medsafety_fhir_r4_{DS}_healthlake_view" in err
    assert err.count("glue delete-database") == 1


def test_no_script_or_template_can_weaken_validation_or_embed_credentials():
    text = "\n".join(p.read_text() for p in [*HL.glob("*.sh"), *HL.glob("policies/*")])
    assert not re.search(r"validation-level\s+(minimal|structure-only)", text)
    assert not re.search(r"AKIA[0-9A-Z]{12,}|aws_secret_access_key|aws_access_key_id", text, re.I)
    assert "AdministratorAccess" not in text
