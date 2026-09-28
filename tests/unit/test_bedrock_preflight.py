"""infrastructure/aws/api/bedrock_check.py -- the decision logic for model account access (stop and report, never switch models)."""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

from app.config import REPO_ROOT

CHECK = REPO_ROOT / "infrastructure" / "aws" / "api" / "bedrock_check.py"
ACCT = "123456789012"
PROFILE_ID, BASE = "us.amazon.nova-2-lite-v1:0", "amazon.nova-2-lite-v1:0"   # Revision 3 target
CLAUDE_PROFILE_ID, CLAUDE_BASE = "us.anthropic.claude-sonnet-5", "anthropic.claude-sonnet-5"   # paused path: strict rules unchanged
GOOD_AVAIL = {"modelId": BASE, "agreementAvailability": {"status": "AVAILABLE"}, "authorizationStatus": "NOT_AUTHORIZED",  # as reported for EVERY Amazon model here
              "entitlementAvailability": "AVAILABLE", "regionAvailability": "AVAILABLE"}
GOOD_PROFILE = {"inferenceProfileId": PROFILE_ID, "inferenceProfileArn": f"arn:aws:bedrock:us-east-1:{ACCT}:inference-profile/{PROFILE_ID}", "status": "ACTIVE",
                "type": "SYSTEM_DEFINED", "models": [{"modelArn": f"arn:aws:bedrock:{r}::foundation-model/{BASE}"} for r in ("us-east-2", "us-east-1", "us-west-2")]}


def run(tmp_path, avail, profile, profile_id=PROFILE_ID, base=BASE):
    (tmp_path / "a.json").write_text(json.dumps(avail))
    (tmp_path / "p.json").write_text(json.dumps(profile))
    return subprocess.run([sys.executable, str(CHECK), str(tmp_path / "a.json"), str(tmp_path / "p.json"), profile_id, base], capture_output=True, text=True)


def test_access_in_place_prints_the_exact_resources_for_the_iam_statements(tmp_path):
    proc = run(tmp_path, GOOD_AVAIL, GOOD_PROFILE)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["profileArn"] == GOOD_PROFILE["inferenceProfileArn"] and out["regions"] == ["us-east-1", "us-east-2", "us-west-2"]
    assert out["foundationModelArns"] == sorted(m["modelArn"] for m in GOOD_PROFILE["models"])


@pytest.mark.parametrize("patch", [
    {"entitlementAvailability": "NOT_AVAILABLE"}, {"regionAvailability": "NOT_AVAILABLE"},
    {"agreementAvailability": {"status": "PENDING"}}, {"agreementAvailability": {"status": "NOT_AVAILABLE", "errorMessage": "Marketplace subscription required"}},
])
def test_any_enablement_gap_is_exit_3_stop_and_report_without_changing_anything(tmp_path, patch):
    proc = run(tmp_path, {**GOOD_AVAIL, **patch}, GOOD_PROFILE)
    assert proc.returncode == 3 and proc.stdout == ""
    assert "ACCOUNT ENABLEMENT REQUIRED" in proc.stderr and "Not changing models, not changing IAM" in proc.stderr


@pytest.mark.parametrize("patch", [{"status": "CREATING"}, {"type": "APPLICATION"}, {"inferenceProfileId": "global.amazon.nova-2-lite-v1:0"},
                                   {"models": []}, {"models": [{"modelArn": "arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-pro-v1:0"}]}])
def test_a_profile_that_is_not_as_planned_is_exit_4(tmp_path, patch):
    proc = run(tmp_path, GOOD_AVAIL, {**GOOD_PROFILE, **patch})
    assert proc.returncode == 4 and proc.stdout == "" and "not as planned" in proc.stderr


def test_amazon_models_report_authorization_status_but_it_is_not_a_stop_condition(tmp_path):
    proc = run(tmp_path, GOOD_AVAIL, GOOD_PROFILE)
    assert GOOD_AVAIL["authorizationStatus"] == "NOT_AUTHORIZED" and proc.returncode == 0
    assert "NOT_AUTHORIZED  (informational for Amazon models: proven at first invocation)" in proc.stderr


def test_the_anthropic_path_keeps_its_strict_rules(tmp_path):
    avail = {**GOOD_AVAIL, "modelId": CLAUDE_BASE, "authorizationStatus": "NOT_AUTHORIZED", "agreementAvailability": {"status": "NOT_AVAILABLE"}}
    profile = {**GOOD_PROFILE, "inferenceProfileId": CLAUDE_PROFILE_ID, "models": [{"modelArn": f"arn:aws:bedrock:us-east-1::foundation-model/{CLAUDE_BASE}"}]}
    proc = run(tmp_path, avail, profile, CLAUDE_PROFILE_ID, CLAUDE_BASE)
    assert proc.returncode == 3 and "ACCOUNT ENABLEMENT REQUIRED" in proc.stderr
    ok = run(tmp_path, {**avail, "authorizationStatus": "AUTHORIZED", "agreementAvailability": {"status": "AVAILABLE"}}, profile, CLAUDE_PROFILE_ID, CLAUDE_BASE)
    assert ok.returncode == 0
    only_auth = run(tmp_path, {**avail, "agreementAvailability": {"status": "AVAILABLE"}}, profile, CLAUDE_PROFILE_ID, CLAUDE_BASE)
    assert only_auth.returncode == 3  # NOT_AUTHORIZED still stops an Anthropic model
