#!/usr/bin/env bash
# Create/update the Lambda function (python3.12, arm64, 512 MB, 30 s, no VPC). 512 MB is this (young) account's Lambda memory limit;
# the plan said 1024 MB. Size only: no IAM or architecture effect.
#
# HEALTHLAKE_WRITE_OUTPUTS defaults to false and stays false unless APPROVE_HEALTHLAKE_WRITE_OUTPUTS=yes is set
# for this specific run -- a separate, explicit, reviewable decision from the app code and IAM grant that make
# write-back *possible* (persist_to_fhir(), the explicit "Save analysis results to HealthLake" action; the
# least-privilege healthlake:UpdateResource statement in lambda-access.json.tpl). Setting the env var alone
# never turns writes on; this script still enforces the deployed value exactly matches what was approved for
# this run (not just "false" always), so a config drift elsewhere can't silently flip it either way unnoticed.
# EXPLANATION_PROVIDER=openai (docs/adr/0011): OPENAI_API_KEY itself is NEVER an env var here -- only
# OPENAI_API_KEY_SECRET_ARN (an ARN, not a secret); backend/app/secrets.py fetches the real key from Secrets
# Manager at runtime, into that same env var, the first time it's needed. Requires 26_openai_secret.sh (Gate C)
# and 27_openai_secret_policy.sh (Gate D) to have already run, so require_state below pulls in OPENAI_SECRET_ARN.
#
# EXPLANATION_MODE=claude (legacy name, predates multi-provider support): means "always attempt the configured
# EXPLANATION_PROVIDER, skip the `auto`-mode credential-presence gate, fall back to the deterministic mock on
# ANY failure" -- see factory.py's create_explanation_service() docstring. It is provider-agnostic despite the
# name: it already deploys this way for provider=bedrock (docs/PHASE4_API.md) and is exercised for
# provider=openai by tests/unit/test_openai_explanation.py::test_explanation_mode_claude_is_provider_agnostic_legacy_naming.
# `auto` mode would also work here (OPENAI_API_KEY_SECRET_ARN is fetched into OPENAI_API_KEY before either
# mode's gate runs), but `claude` is kept for consistency with the already-deployed Bedrock configuration.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
load_identity; load_healthlake_state; require_state ROLE_ARN ZIP_PATH OPENAI_SECRET_ARN
require_current_healthlake_datastore   # refuses here if HealthLake hasn't actually been recreated yet (see lib.sh)

WRITE_OUTPUTS="false"
if [ "${APPROVE_HEALTHLAKE_WRITE_OUTPUTS:-}" = "yes" ]; then
  WRITE_OUTPUTS="true"
  log "APPROVE_HEALTHLAKE_WRITE_OUTPUTS=yes: deploying with HEALTHLAKE_WRITE_OUTPUTS=true -- FHIR write-back is enabled for the explicit persist path only (normal analysis stays read-only regardless)"
fi

python3 - "$STATE_DIR/rendered/lambda-env.json" "$DS_ID" "$OPENAI_SECRET_ARN" "$OPENAI_MODEL_ID" "$DELETED_DS_ID" "$WRITE_OUTPUTS" <<'PY'
import json, sys

env_path, ds_id, secret_arn, model_id, deleted_ds_id, write_outputs = sys.argv[1:7]
env = {"DATA_BACKEND": "healthlake", "HEALTHLAKE_DATASTORE_ID": ds_id, "HEALTHLAKE_REGION": "us-east-1",
       "HEALTHLAKE_WRITE_OUTPUTS": write_outputs, "APP_STATE_BACKEND": "dynamodb", "APP_STATE_TABLE": "medsafety-app-state",
       "EXPLANATION_MODE": "claude", "EXPLANATION_PROVIDER": "openai", "EXPLANATION_MODEL": model_id,
       "OPENAI_API_KEY_SECRET_ARN": secret_arn, "OPENAI_TIMEOUT_SECONDS": "20", "OPENAI_MAX_TOKENS": "4000",
       "DATA_PACKAGE_DIR": "/var/task/data/phase0_v1_0",
       "CORS_ORIGINS": "http://localhost:5173,http://127.0.0.1:5173", "CAPTURE_REJECTED_EXPLANATIONS": "true", "LOG_LEVEL": "INFO"}

# The complete required set. AWS's update-function-configuration REPLACES the entire Variables map on every
# call -- there is no merge -- so a missing key here doesn't just fail this build, it silently DELETES that
# variable from the already-deployed function the moment this script runs. An unexpected extra key is just as
# much a bug (nothing in this script should ever add one) so the check is an exact set match, not a subset check.
REQUIRED_KEYS = {"DATA_BACKEND", "HEALTHLAKE_DATASTORE_ID", "HEALTHLAKE_REGION", "HEALTHLAKE_WRITE_OUTPUTS",
                 "APP_STATE_BACKEND", "APP_STATE_TABLE", "EXPLANATION_MODE", "EXPLANATION_PROVIDER", "EXPLANATION_MODEL",
                 "OPENAI_API_KEY_SECRET_ARN", "OPENAI_TIMEOUT_SECONDS", "OPENAI_MAX_TOKENS", "DATA_PACKAGE_DIR",
                 "CORS_ORIGINS", "CAPTURE_REJECTED_EXPLANATIONS", "LOG_LEVEL"}


def stop(msg):
    print(f"STOP: {msg}", file=sys.stderr)
    raise SystemExit(1)


if set(env) != REQUIRED_KEYS:
    stop(f"lambda environment key set mismatch: missing={sorted(REQUIRED_KEYS - set(env))} extra={sorted(set(env) - REQUIRED_KEYS)}")
if "OPENAI_API_KEY" in env:
    stop("OPENAI_API_KEY must never be a literal Lambda env var -- only OPENAI_API_KEY_SECRET_ARN (an ARN, not a secret)")
if not env["OPENAI_API_KEY_SECRET_ARN"]:
    stop("OPENAI_API_KEY_SECRET_ARN is empty")
if env["HEALTHLAKE_WRITE_OUTPUTS"] not in ("false", "true"):
    stop(f"HEALTHLAKE_WRITE_OUTPUTS is {env['HEALTHLAKE_WRITE_OUTPUTS']!r}, must be 'false' or 'true'")
if not env["HEALTHLAKE_DATASTORE_ID"]:
    stop("HEALTHLAKE_DATASTORE_ID is empty")
if env["HEALTHLAKE_DATASTORE_ID"] == deleted_ds_id:
    stop(f"HEALTHLAKE_DATASTORE_ID equals the DELETED datastore id recorded in datastore-recreation-record.json ({deleted_ds_id})")

json.dump({"Variables": env}, open(env_path, "w"), indent=2)
PY
env_file="file://$STATE_DIR/rendered/lambda-env.json"
# Provider-independent by design (docs/adr/0001, docs/adr/0011): the function's job is unchanged by which
# EXPLANATION_PROVIDER is configured, so the description never names one -- it must stay correct across a
# Bedrock<->OpenAI switch without editing this string. Applied on BOTH the create and update paths (a
# pre-existing gap: the update path used to omit --description entirely, leaving whatever text the function
# was originally created with -- e.g. a now-stale Bedrock-era description -- displayed after every OpenAI
# redeploy).
DESCRIPTION="FHIR Medication Safety Intelligence API -- HealthLake, deterministic safety rules, and guarded AI explanations"
if aws_ro lambda get-function --function-name "$FUNCTION" >/dev/null 2>&1; then
  aws_mut lambda update-function-code --function-name "$FUNCTION" --zip-file "fileb://$ZIP_PATH" --architectures arm64 >/dev/null
  [ "${DRY_RUN:-0}" = "1" ] || aws_ro lambda wait function-updated-v2 --function-name "$FUNCTION"
  aws_mut lambda update-function-configuration --function-name "$FUNCTION" --role "$ROLE_ARN" --handler app.lambda_handler.handler \
    --runtime python3.12 --timeout 30 --memory-size 512 --environment "$env_file" --description "$DESCRIPTION" >/dev/null
elif [ "${DRY_RUN:-0}" = "1" ]; then
  aws_mut lambda create-function --function-name "$FUNCTION" --runtime python3.12 --architectures arm64 --role "$ROLE_ARN" \
    --handler app.lambda_handler.handler --zip-file "fileb://$ZIP_PATH" --timeout 30 --memory-size 512 --environment "$env_file" \
    --description "$DESCRIPTION" --tags "$(tags_kv)"
else
  for attempt in 1 2 3 4 5 6; do   # a brand-new role can take a few seconds to become assumable
    if aws lambda create-function --function-name "$FUNCTION" --runtime python3.12 --architectures arm64 --role "$ROLE_ARN" \
        --handler app.lambda_handler.handler --zip-file "fileb://$ZIP_PATH" --timeout 30 --memory-size 512 --environment "$env_file" \
        --description "$DESCRIPTION" --tags "$(tags_kv)" >/dev/null 2>"$STATE_DIR/create-fn.err"; then break; fi
    grep -q "cannot be assumed" "$STATE_DIR/create-fn.err" && [ "$attempt" -lt 6 ] && { log "role not assumable yet; retrying"; sleep 10; continue; }
    cat "$STATE_DIR/create-fn.err" >&2; die "lambda create-function failed"
  done
fi
[ "${DRY_RUN:-0}" = "1" ] || aws_ro lambda wait function-active-v2 --function-name "$FUNCTION"
if [ "${DRY_RUN:-0}" != "1" ]; then
  wo="$(aws_ro lambda get-function-configuration --function-name "$FUNCTION" --query 'Environment.Variables.HEALTHLAKE_WRITE_OUTPUTS' --output text)"
  # Must match exactly what THIS run approved -- not just "always false" -- so neither an accidental true
  # (missing approval didn't stick) nor an accidental false (approval given but not applied) goes unnoticed.
  [ "$wo" = "$WRITE_OUTPUTS" ] || die "HEALTHLAKE_WRITE_OUTPUTS is '$wo' on the deployed function; expected '$WRITE_OUTPUTS' (the value approved for this run)"
fi
save_state FUNCTION_ARN "arn:aws:lambda:us-east-1:$ACCT:function:$FUNCTION"
log "function ready: $FUNCTION (HEALTHLAKE_WRITE_OUTPUTS=$WRITE_OUTPUTS)"
