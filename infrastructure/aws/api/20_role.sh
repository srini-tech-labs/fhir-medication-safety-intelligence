#!/usr/bin/env bash
# Lambda execution role WITHOUT any Bedrock statement (that is Gate B, 25_bedrock_policy.sh). HealthLake: read + search only.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
load_identity; load_healthlake_state
export ACCT DS KEY_ARN TABLE LOG_GROUP
mapfile -t TAGS < <(tags_cli)
access="$(render lambda-access.json.tpl lambda-access.json)"
if aws_ro iam get-role --role-name "$ROLE" >/dev/null 2>&1; then
  aws_mut iam update-assume-role-policy --role-name "$ROLE" --policy-document "file://$API_DIR/policies/lambda-trust.json"
else
  aws_mut iam create-role --role-name "$ROLE" --assume-role-policy-document "file://$API_DIR/policies/lambda-trust.json" \
    --tags "${TAGS[@]}" --description "MedSafety Phase 4 API Lambda (read-only HealthLake)" >/dev/null
fi
aws_mut iam put-role-policy --role-name "$ROLE" --policy-name BaseAccess --policy-document "file://$access"
save_state ROLE_ARN "arn:aws:iam::$ACCT:role/$ROLE"
log "role ready: $ROLE (no Bedrock statement yet)"
