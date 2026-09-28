"""scripts/aws_identity_check.sh against a stub `aws` (offline): placeholder guard, STS-only calls, expected ARNs, no secret output.

Fully self-contained: every AWS config/profile file the script reads is written fresh into a pytest tmp_path
with fake values (never copied from the real, machine-local .env.aws / .aws-local/), so this suite needs no
real AWS credentials, no project-local AWS setup and no access to any AWS account -- it passes on a completely
fresh clone. The one test that genuinely checks the developer's own real .env.aws/.aws-local/ (not a fixture)
is marked live_aws and skipped by default, exactly like the project's other opt-in live checks."""
from __future__ import annotations

import json
import os
import stat
import subprocess

import pytest

from app.config import REPO_ROOT

SCRIPT = REPO_ROOT / "scripts" / "aws_identity_check.sh"
ACCT = "123456789012"
SECRET = "ZZ-FAKE-SECRET-VALUE-DO-NOT-LEAK-0123456789"
KEYID = "AKIAFAKEKEYIDFORTEST01"

STUB = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
open(os.environ["STUB_LOG"], "a").write(json.dumps([os.environ.get("AWS_PROFILE", "")] + args) + "\n")
if args[:2] == ["configure", "get"]:
    prof = args[args.index("--profile") + 1]; key = args[2]
    print({("region"): "us-east-1", "role_arn": "arn:aws:iam::123456789012:role/MedSafetyPhase3DeployerRole",
           "source_profile": "medsafety-bootstrap"}.get(key, "")); sys.exit(0)
if args[:2] == ["sts", "get-caller-identity"]:
    prof = os.environ["AWS_PROFILE"]
    if prof == "medsafety-bootstrap":
        print(os.environ.get("STUB_BOOTSTRAP", "123456789012 arn:aws:iam::123456789012:user/medsafety-bootstrap")); sys.exit(0)
    if os.environ.get("STUB_ROLE_FAIL"):
        sys.stderr.write("An error occurred (AccessDenied) when calling the AssumeRole operation: not authorized\n"); sys.exit(254)
    print("123456789012 arn:aws:sts::123456789012:assumed-role/MedSafetyPhase3DeployerRole/medsafety-phase3"); sys.exit(0)
sys.stderr.write("UNEXPECTED AWS CALL " + " ".join(args) + "\n"); sys.exit(99)
'''


@pytest.fixture()
def project(tmp_path):
    root = tmp_path / "proj"
    (root / "scripts").mkdir(parents=True)
    (root / ".aws-local").mkdir()
    # Fake, self-written project-local AWS config -- same shape as the real (git-ignored, never-committed)
    # .env.aws / .aws-local/config this script expects, but entirely synthetic: fabricated inside tmp_path so
    # this test needs no real machine-local AWS setup. Content is illustrative only where it matters for the
    # script's own file-existence/path checks below; the stubbed `aws` further down intercepts every
    # `aws configure get` call and never actually parses this file, so its profile values don't need to be
    # realistic beyond matching what the stub itself returns.
    (root / ".env.aws").write_text(
        'export AWS_CONFIG_FILE="$PWD/.aws-local/config"\n'
        'export AWS_SHARED_CREDENTIALS_FILE="$PWD/.aws-local/credentials"\n'
        'export AWS_PROFILE=medsafety\n'
        'export AWS_REGION=us-east-1\n'
        'export AWS_DEFAULT_REGION=us-east-1\n'
        'export AWS_PAGER=""\n'
    )
    (root / ".aws-local" / "config").write_text(
        "[profile medsafety-bootstrap]\nregion = us-east-1\n\n"
        f"[profile medsafety]\nregion = us-east-1\nrole_arn = arn:aws:iam::{ACCT}:role/MedSafetyPhase3DeployerRole\n"
        "source_profile = medsafety-bootstrap\n"
    )
    (root / ".gitignore").write_text(".aws-local/\n.env.aws\n")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "aws").write_text(STUB)
    (bindir / "aws").chmod((bindir / "aws").stat().st_mode | stat.S_IEXEC)
    return {"root": root, "bin": bindir, "log": tmp_path / "aws.log"}


def creds(project, *, placeholder: bool):
    body = ("[medsafety-bootstrap]\naws_access_key_id = REPLACE_LOCALLY\naws_secret_access_key = REPLACE_LOCALLY\n" if placeholder else
            f"[medsafety-bootstrap]\naws_access_key_id = {KEYID}\naws_secret_access_key = {SECRET}\n")
    (project["root"] / ".aws-local" / "credentials").write_text(body)


def run(project, **env):
    e = {k: v for k, v in os.environ.items() if not k.startswith("AWS_")}
    e.update({"PATH": f"{project['bin']}:{os.environ['PATH']}", "PROJECT_ROOT": str(project["root"]), "STUB_LOG": str(project["log"]), **env})
    return subprocess.run(["bash", str(SCRIPT)], env=e, capture_output=True, text=True, timeout=60, cwd=project["root"])


def calls(project):
    return [json.loads(l) for l in project["log"].read_text().splitlines()] if project["log"].exists() else []


def test_placeholders_stop_before_any_aws_call(project):
    creds(project, placeholder=True)
    proc = run(project)
    assert proc.returncode == 3 and "Add the bootstrap Access Key ID and Secret Access Key directly to .aws-local/credentials" in proc.stdout
    assert calls(project) == []  # no AWS call at all with placeholders


def test_success_path_makes_only_sts_identity_calls_and_never_prints_secrets(project):
    creds(project, placeholder=False)
    proc = run(project)
    assert proc.returncode == 0, proc.stderr
    aws_calls = calls(project)
    assert {tuple(c[1:3]) for c in aws_calls} == {("configure", "get"), ("sts", "get-caller-identity")}
    assert [c[0] for c in aws_calls if c[1] == "sts"] == ["medsafety-bootstrap", "medsafety"]
    for text in (proc.stdout, proc.stderr):
        assert SECRET not in text and KEYID not in text
    for phrase in ("bootstrap identity verified", "account 123456789012 verified", "deployer-role assumption verified", "region us-east-1", "ignored by git"):
        assert phrase in proc.stdout


def test_wrong_bootstrap_identity_stops_before_role_assumption(project):
    creds(project, placeholder=False)
    proc = run(project, STUB_BOOTSTRAP=f"{ACCT} arn:aws:iam::{ACCT}:user/someone-else")
    assert proc.returncode == 1 and "expected arn:aws:iam::123456789012:user/medsafety-bootstrap" in proc.stderr
    assert [c[0] for c in calls(project) if c[1] == "sts"] == ["medsafety-bootstrap"]


def test_role_assumption_failure_is_reported_and_nothing_is_changed(project):
    creds(project, placeholder=False)
    proc = run(project, STUB_ROLE_FAIL="1")
    assert proc.returncode == 1 and "deployer-role assumption FAILED" in proc.stderr and "AccessDenied" in proc.stderr
    assert all(c[1] in ("configure", "sts") for c in calls(project))  # read-only calls only


def test_ambient_credentials_are_refused(project):
    creds(project, placeholder=False)
    proc = run(project, AWS_ACCESS_KEY_ID="AKIAAMBIENT")
    assert proc.returncode == 1 and "AWS_ACCESS_KEY_ID is set" in proc.stderr and calls(project) == []


def test_unignored_credentials_are_refused(project):
    creds(project, placeholder=False)
    (project["root"] / ".gitignore").write_text("")
    proc = run(project)
    assert proc.returncode == 1 and "NOT ignored by git" in proc.stderr and calls(project) == []


@pytest.mark.live_aws  # not an AWS network call, but genuinely requires the developer's own real, machine-local
# .env.aws / .aws-local/ (never part of a fresh clone). Reuses the project's existing opt-in marker so it is
# deselected by default (pytest.ini: -m "not live_aws") and only runs when explicitly selected
# (pytest -m live_aws), once that local project-local AWS setup actually exists.
def test_repo_credential_files_are_ignored_untracked_and_private():
    for f in (".aws-local/credentials", ".aws-local/config", ".env.aws"):
        assert subprocess.run(["git", "check-ignore", "-q", f], cwd=REPO_ROOT).returncode == 0, f
    assert subprocess.run(["git", "ls-files", ".aws-local", ".env.aws"], cwd=REPO_ROOT, capture_output=True, text=True).stdout == ""
    assert stat.S_IMODE((REPO_ROOT / ".aws-local").stat().st_mode) == 0o700
    for f in (".aws-local/credentials", ".aws-local/config", ".env.aws"):
        assert stat.S_IMODE((REPO_ROOT / f).stat().st_mode) == 0o600, f


def test_only_sts_calls_exist_in_the_script():
    import re

    text = SCRIPT.read_text()
    assert set(re.findall(r"\baws (sts) ([\w-]+)", text)) == {("sts", "get-caller-identity")}
    assert not re.search(r"\baws (s3|s3api|iam|kms|healthlake|cloudtrail|ec2|lambda|sso|organizations|configure (set|import)|login)\b", text)
