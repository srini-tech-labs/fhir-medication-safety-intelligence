#!/usr/bin/env bash
# DynamoDB app-state table (DEFAULT AWS-owned encryption: no --sse-specification, no KMS) + the Lambda log group (30-day retention).
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
load_identity
mapfile -t TAGS < <(tags_cli)
if ! aws_ro dynamodb describe-table --table-name "$TABLE" >/dev/null 2>&1; then
  aws_mut dynamodb create-table --table-name "$TABLE" \
    --attribute-definitions AttributeName=pk,AttributeType=S AttributeName=sk,AttributeType=S \
    --key-schema AttributeName=pk,KeyType=HASH AttributeName=sk,KeyType=RANGE \
    --billing-mode PAY_PER_REQUEST --table-class STANDARD --tags "${TAGS[@]}" >/dev/null
  [ "${DRY_RUN:-0}" = "1" ] || aws_ro dynamodb wait table-exists --table-name "$TABLE"
fi
aws_mut dynamodb update-time-to-live --table-name "$TABLE" --time-to-live-specification Enabled=true,AttributeName=ttl >/dev/null || log "TTL already enabled"
if [ "${DRY_RUN:-0}" != "1" ]; then
  desc="$(aws_ro dynamodb describe-table --table-name "$TABLE" --output json)"
  python3 - "$desc" <<'PY'
import json, sys
t = json.loads(sys.argv[1])["Table"]
assert t["TableStatus"] == "ACTIVE", t["TableStatus"]
assert "SSEDescription" not in t, f"table is not using the default AWS-owned encryption: {t.get('SSEDescription')}"
assert t.get("BillingModeSummary", {}).get("BillingMode") == "PAY_PER_REQUEST"
assert [k["AttributeName"] for k in t["KeySchema"]] == ["pk", "sk"]
print("table ACTIVE, on-demand, default encryption (no SSEDescription)", file=sys.stderr)
PY
fi
if [ -z "$(aws_ro logs describe-log-groups --log-group-name-prefix "$LOG_GROUP" --query 'logGroups[?logGroupName==`'"$LOG_GROUP"'`].logGroupName' --output text)" ]; then
  aws_mut logs create-log-group --log-group-name "$LOG_GROUP" --tags "$(tags_kv)"
fi
aws_mut logs put-retention-policy --log-group-name "$LOG_GROUP" --retention-in-days 30
save_state TABLE "$TABLE"; save_state LOG_GROUP "$LOG_GROUP"
log "state ready: table $TABLE, log group $LOG_GROUP (30 days)"
