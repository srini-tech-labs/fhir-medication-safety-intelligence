#!/usr/bin/env bash
# GATE B. Adds the Bedrock statements (InvokeModel on the Nova 2 Lite profile + member models, GetInferenceProfile on the profile) to the Lambda role. Refuses to run unless the operator approved the exact statements
# printed by 00_preflight.sh:   APPROVE_BEDROCK_POLICY=yes ./25_bedrock_policy.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
load_identity; require_state PROFILE_ARN FOUNDATION_MODEL_ARNS_JSON
export ACCT PROFILE_ARN FOUNDATION_MODEL_ARNS_JSON
statements="$(render lambda-bedrock.json.tpl lambda-bedrock.json)"
cat "$statements" >&2; echo >&2
[ "${APPROVE_BEDROCK_POLICY:-}" = "yes" ] || die "Gate B: set APPROVE_BEDROCK_POLICY=yes only after the operator approved the statements above"
aws_mut iam put-role-policy --role-name "$ROLE" --policy-name BedrockInvoke --policy-document "file://$statements"
log "Bedrock invoke statements applied to $ROLE"
