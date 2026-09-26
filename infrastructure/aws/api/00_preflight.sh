#!/usr/bin/env bash
# READ-ONLY. Creates nothing. Confirms identity, the (unchanged) datastore, that every Phase 4 name is free, and the ACTUAL Sonnet 5 access
# in this account (availability, Marketplace agreement / entitlement, US geo inference profile and its member regions).
# Exit 3 = account enablement needed; exit 4 = inference profile not as planned. Both: stop and report, never change model or IAM.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
load_identity; load_healthlake_state
log "identity: $IDENTITY_ARN  account $ACCT  region $AWS_REGION"

status="$(aws_ro healthlake describe-fhir-datastore --datastore-id "$DS_ID" --query DatastoreProperties.DatastoreStatus --output text)"
[ "$status" = ACTIVE ] || die "datastore $DS_ID is $status, expected ACTIVE"
log "datastore $DS_ID ACTIVE (read-only check; Phase 4 never modifies it)"

expect_missing "DynamoDB table $TABLE"   "ResourceNotFoundException"  dynamodb describe-table --table-name "$TABLE"
expect_missing "IAM role $ROLE"          "NoSuchEntity"               iam get-role --role-name "$ROLE"
expect_missing "Lambda function $FUNCTION" "ResourceNotFoundException" lambda get-function --function-name "$FUNCTION"
[ -z "$(aws_ro logs describe-log-groups --log-group-name-prefix "$LOG_GROUP" --query 'logGroups[].logGroupName' --output text)" ] || die "unexpected existing log group $LOG_GROUP"
[ -z "$(aws_ro apigateway get-rest-apis --query "items[?name=='$API_NAME'].id" --output text)" ] || die "unexpected existing REST API named $API_NAME"
log "all Phase 4 names are free"

tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
aws_ro bedrock get-foundation-model-availability --model-id "$BASE_MODEL_ID" --output json > "$tmp/avail.json"
aws_ro bedrock get-inference-profile --inference-profile-identifier "$MODEL_ID" --output json > "$tmp/profile.json"
aws_ro bedrock list-foundation-model-agreement-offers --model-id "$BASE_MODEL_ID" --output json > "$tmp/offers.json" 2>/dev/null \
  && log "agreement offers: $(python3 -c "import json;print(len(json.load(open('$tmp/offers.json')).get('offers',[])))") listed" || log "agreement offers: not readable (informational)"
if [[ "$BASE_MODEL_ID" == anthropic.* ]]; then   # the first-time-use form is an Anthropic-only requirement
  aws_ro bedrock get-use-case-for-model-access --output json > "$tmp/usecase.json" 2>/dev/null \
    && log "first-time-use form: submitted" || log "first-time-use form: none on record / not readable (informational)"
fi

set +e; python3 "$API_DIR/bedrock_check.py" "$tmp/avail.json" "$tmp/profile.json" "$MODEL_ID" "$BASE_MODEL_ID" > "$STATE_DIR/bedrock-preflight.json"; rc=$?; set -e
[ "$rc" = 0 ] || exit "$rc"
save_state PROFILE_ARN "$(python3 -c "import json;print(json.load(open('$STATE_DIR/bedrock-preflight.json'))['profileArn'])")"
save_state FOUNDATION_MODEL_ARNS_JSON "$(python3 -c "import json;print(json.dumps(json.load(open('$STATE_DIR/bedrock-preflight.json'))['foundationModelArns']))")"
require_state PROFILE_ARN FOUNDATION_MODEL_ARNS_JSON
export PROFILE_ARN FOUNDATION_MODEL_ARNS_JSON
log "GATE B statements (NOT applied; shown for your approval):"
cat "$(render lambda-bedrock.json.tpl lambda-bedrock.json)" >&2; echo >&2
log "PREFLIGHT OK: read-only, nothing created. Invocation of $MODEL_ID is only proven by the first real call at Gate B."
