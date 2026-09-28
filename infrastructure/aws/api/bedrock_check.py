#!/usr/bin/env python3
"""Interpret the READ-ONLY Bedrock preflight outputs for a model reached through a US geo inference profile.

    bedrock_check.py AVAILABILITY.json PROFILE.json EXPECTED_PROFILE_ID BASE_MODEL_ID > result.json

Exit 0: model access is in place; prints {profileArn, foundationModelArns, regions} (the exact resources for the Bedrock IAM
        statements, shown to the operator BEFORE anything is applied).
Exit 3: ACCOUNT ENABLEMENT REQUIRED (authorization / entitlement / agreement / region availability not satisfied) -> stop and report.
        Amazon-owned models (Nova) are not sold through AWS Marketplace and need no first-time-use form (AWS: access is enabled by
        default); every Amazon model in this account reports authorizationStatus NOT_AUTHORIZED, so for the `amazon.` provider that
        field is REPORTED but not a stop condition. The definitive proof is the first real invocation at Gate B. Anthropic keeps
        the strict rule (authorization must be AUTHORIZED).
Exit 4: the inference profile is not what was planned -> stop and report.
Nothing here changes a model or an IAM policy.
"""
from __future__ import annotations

import json
import sys


def main(argv: list[str]) -> int:
    avail_path, profile_path, profile_id, base_model = argv
    avail, profile = json.load(open(avail_path)), json.load(open(profile_path))
    amazon = base_model.startswith("amazon.")
    checks = {
        "authorizationStatus": (avail.get("authorizationStatus"), "AUTHORIZED"),
        "entitlementAvailability": (avail.get("entitlementAvailability"), "AVAILABLE"),
        "agreementAvailability.status": ((avail.get("agreementAvailability") or {}).get("status"), "AVAILABLE"),
        "regionAvailability": (avail.get("regionAvailability"), "AVAILABLE"),
    }
    informational = {"authorizationStatus"} if amazon else set()
    print(f"model {base_model} availability in this account/region:", file=sys.stderr)
    for name, (got, want) in checks.items():
        flag = "OK" if got == want else ("(informational for Amazon models: proven at first invocation)" if name in informational else "<-- expected " + want)
        print(f"  {name:32} {got}  {flag}", file=sys.stderr)
    if any(got != want for name, (got, want) in checks.items() if name not in informational):
        err = (avail.get("agreementAvailability") or {}).get("errorMessage")
        print("STOP: ACCOUNT ENABLEMENT REQUIRED for this model (AWS Marketplace agreement / first-time-use / entitlement)."
              " Not changing models, not changing IAM." + (f" AWS says: {err}" if err else ""), file=sys.stderr)
        return 3
    models = [m.get("modelArn", "") for m in profile.get("models", [])]
    problems = []
    if profile.get("inferenceProfileId") != profile_id:
        problems.append(f"profile id {profile.get('inferenceProfileId')!r} != {profile_id!r}")
    if profile.get("status") != "ACTIVE":
        problems.append(f"profile status {profile.get('status')!r}")
    if profile.get("type") != "SYSTEM_DEFINED":
        problems.append(f"profile type {profile.get('type')!r}")
    if not models or any(not m.endswith(f"foundation-model/{base_model}") for m in models):
        problems.append(f"profile models are not all {base_model}: {models}")
    if problems:
        print("STOP: inference profile is not as planned: " + "; ".join(problems), file=sys.stderr)
        return 4
    regions = sorted({m.split(":")[3] for m in models})
    print(f"profile {profile_id}: ACTIVE, SYSTEM_DEFINED, routes to {regions}", file=sys.stderr)
    print(json.dumps({"profileArn": profile["inferenceProfileArn"], "foundationModelArns": sorted(models), "regions": regions}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
