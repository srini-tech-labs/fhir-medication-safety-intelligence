#!/usr/bin/env bash
# V0-V19 direct FHIR verification as the Verifier role + live golden parity as the App role.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
load_identity; require_state DS_ID APP_ROLE_ARN VERIFIER_ROLE_ARN RESULTS_BUCKET
PY="$REPO_ROOT/backend/.venv/bin/python"
"$PY" "$REPO_ROOT/scripts/healthlake_verify.py" --datastore-id "$DS_ID" --verifier-role-arn "$VERIFIER_ROLE_ARN" \
    --app-role-arn "$APP_ROLE_ARN" --out "$STATE_DIR/verification-report.json"
RUN_HEALTHLAKE_LIVE=1 HEALTHLAKE_DATASTORE_ID="$DS_ID" HEALTHLAKE_ROLE_ARN="$APP_ROLE_ARN" \
    "$PY" -m pytest -m live_aws tests/live_aws -o addopts="-q" 2>&1 | tail -20
