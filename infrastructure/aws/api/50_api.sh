#!/usr/bin/env bash
# REST API (regional) with EXACTLY the contract routes (routes.txt), AWS_PROXY to the one Lambda, source-IP resource policy, throttling,
# stage `dev`. Requires ALLOWED_IP_CIDR=<your public IPv4>/32 (confirmed by the operator).
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
load_identity; require_state FUNCTION_ARN
[[ "${ALLOWED_IP_CIDR:-}" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}/32$ ]] || die "set ALLOWED_IP_CIDR to your public IPv4 as a /32 (e.g. 203.0.113.7/32); open ranges are refused"
export ACCT ALLOWED_IP_CIDR
policy_file="$(render api-resource-policy.json.tpl api-resource-policy.json)"

API_ID="$(aws_ro apigateway get-rest-apis --query "items[?name=='$API_NAME'].id | [0]" --output text)"
if [ -z "$API_ID" ] || [ "$API_ID" = None ]; then
  API_ID="$(aws_mut apigateway create-rest-api --name "$API_NAME" --description "MedSafety Phase 4 API" --endpoint-configuration types=REGIONAL \
      --policy "$(cat "$policy_file")" --tags "$(tags_kv)" --query id --output text)"
fi
[ "${DRY_RUN:-0}" = "1" ] && API_ID="${API_ID:-DRYRUN}"
save_state API_ID "$API_ID"
ROOT_ID="$(aws_ro apigateway get-resources --rest-api-id "$API_ID" --query "items[?path=='/'].id | [0]" --output text 2>/dev/null || echo ROOT)"
LAMBDA_URI="arn:aws:apigateway:us-east-1:lambda:path/2015-03-31/functions/$FUNCTION_ARN/invocations"

resource_id() { # resource_id PARENT_ID PATH_PART -> id (creates when missing)
  local parent="$1" part="$2" id
  id="$(aws_ro apigateway get-resources --rest-api-id "$API_ID" --query "items[?parentId=='$parent' && pathPart=='$part'].id | [0]" --output text 2>/dev/null || true)"
  if [ -z "$id" ] || [ "$id" = None ]; then id="$(aws_mut apigateway create-resource --rest-api-id "$API_ID" --parent-id "$parent" --path-part "$part" --query id --output text)"; fi
  printf '%s' "${id:-DRYRUN}"
}
mut_idempotent() { # like aws_mut, but an already-existing method (ConflictException) is fine on a re-run
  if [ "${DRY_RUN:-0}" = "1" ]; then aws_mut "$@"; return; fi
  local out; if ! out="$(aws "$@" 2>&1)"; then grep -q ConflictException <<<"$out" || { printf '%s\n' "$out" >&2; return 1; }; fi
}
add_method() { # add_method RESOURCE_ID HTTP_METHOD
  mut_idempotent apigateway put-method --rest-api-id "$API_ID" --resource-id "$1" --http-method "$2" --authorization-type NONE >/dev/null
  aws_mut apigateway put-integration --rest-api-id "$API_ID" --resource-id "$1" --http-method "$2" --type AWS_PROXY \
    --integration-http-method POST --uri "$LAMBDA_URI" --timeout-in-millis 29000 >/dev/null
}
while read -r method path; do
  [[ "$method" =~ ^(GET|POST)$ ]] || continue
  rid="$ROOT_ID"; IFS=/ read -ra parts <<<"${path#/}"
  for part in "${parts[@]}"; do rid="$(resource_id "$rid" "$part")"; done
  add_method "$rid" "$method"
  add_method "$rid" OPTIONS          # CORS preflight, answered by FastAPI's CORS middleware
  log "route $method $path (+OPTIONS)"
done < <(grep -vE '^\s*(#|$)' "$API_DIR/routes.txt")

mut_idempotent lambda add-permission --function-name "$FUNCTION" --statement-id apigw-invoke --action lambda:InvokeFunction \
  --principal apigateway.amazonaws.com --source-arn "arn:aws:execute-api:us-east-1:$ACCT:$API_ID/*/*/*" >/dev/null   # ResourceConflictException = already present
aws_mut apigateway create-deployment --rest-api-id "$API_ID" --stage-name "$STAGE" --description "Phase 4" >/dev/null
# Stage throttling. JSON (not shorthand): route paths contain {braces}, which the CLI shorthand parser rejects.
EXPL='/~1v1~1patients~1{patient_id}~1analyses~1{analysis_id}~1explanation/POST/throttling'   # "/" is written ~1 in a method-settings key
python3 - "$STATE_DIR/rendered/stage-patch.json" "$EXPL" <<'PY'
import json, sys
expl = sys.argv[2]
ops = [("/*/*/throttling/rateLimit", "5"), ("/*/*/throttling/burstLimit", "10"),
       (expl + "/rateLimit", "2"), (expl + "/burstLimit", "3")]
json.dump([{"op": "replace", "path": p, "value": v} for p, v in ops], open(sys.argv[1], "w"), indent=1)
PY
aws_mut apigateway update-stage --rest-api-id "$API_ID" --stage-name "$STAGE" --patch-operations "file://$STATE_DIR/rendered/stage-patch.json" >/dev/null
aws_mut apigateway tag-resource --resource-arn "arn:aws:apigateway:us-east-1::/restapis/$API_ID/stages/$STAGE" --tags "$(tags_kv)" >/dev/null 2>&1 || true
save_state INVOKE_URL "https://$API_ID.execute-api.us-east-1.amazonaws.com/$STAGE"
log "API ready: https://$API_ID.execute-api.us-east-1.amazonaws.com/$STAGE  (allowed source: $ALLOWED_IP_CIDR)"
