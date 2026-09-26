#!/usr/bin/env bash
# Live verification over HTTPS (run from the allow-listed IP): A0-A16.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
load_identity; load_healthlake_state; require_state INVOKE_URL VERIFIER_ROLE_ARN
"$REPO_ROOT/backend/.venv/bin/python" "$REPO_ROOT/scripts/api_verify.py" --mode http --base-url "$INVOKE_URL" \
  --datastore-id "$DS_ID" --verifier-role-arn "$VERIFIER_ROLE_ARN" --function "$FUNCTION" --table "$TABLE" --log-group "$LOG_GROUP" --role-name "$ROLE" \
  --out "$STATE_DIR/api-verification-report.json" "$@"
