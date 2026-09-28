#!/usr/bin/env bash
# DESTRUCTIVE (Phase 4 only). Deletes the API, Lambda, role, table and log group. NEVER touches HealthLake, KMS, buckets or Phase 3 roles.
#   CONFIRM_TEARDOWN=yes ./90_teardown.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
load_identity; load_state
[ "${CONFIRM_TEARDOWN:-}" = "yes" ] || die "set CONFIRM_TEARDOWN=yes to delete the Phase 4 resources (the user must approve first)"
API_ID="${API_ID:-$(aws_ro apigateway get-rest-apis --query "items[?name=='$API_NAME'].id | [0]" --output text 2>/dev/null || true)}"
if [ -n "${API_ID:-}" ] && [ "$API_ID" != None ]; then aws_mut apigateway delete-rest-api --rest-api-id "$API_ID"; fi
if aws_ro lambda get-function --function-name "$FUNCTION" >/dev/null 2>&1; then aws_mut lambda delete-function --function-name "$FUNCTION"; fi
if aws_ro iam get-role --role-name "$ROLE" >/dev/null 2>&1; then
  for P in $(aws_ro iam list-role-policies --role-name "$ROLE" --query PolicyNames --output text); do aws_mut iam delete-role-policy --role-name "$ROLE" --policy-name "$P"; done
  aws_mut iam delete-role --role-name "$ROLE"
fi
if aws_ro dynamodb describe-table --table-name "$TABLE" >/dev/null 2>&1; then aws_mut dynamodb delete-table --table-name "$TABLE" >/dev/null; fi
[ -z "$(aws_ro logs describe-log-groups --log-group-name-prefix "$LOG_GROUP" --query 'logGroups[].logGroupName' --output text)" ] || aws_mut logs delete-log-group --log-group-name "$LOG_GROUP"
log "Phase 4 teardown finished (HealthLake, KMS, buckets and Phase 3 roles untouched)"
