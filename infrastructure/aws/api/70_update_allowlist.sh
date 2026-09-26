#!/usr/bin/env bash
# Re-render the source-IP resource policy after your public IP changed:   ALLOWED_IP_CIDR=203.0.113.9/32 ./70_update_allowlist.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
load_identity; require_state API_ID
[[ "${ALLOWED_IP_CIDR:-}" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}/32$ ]] || die "set ALLOWED_IP_CIDR to a single public IPv4 /32; open ranges are refused"
export ACCT ALLOWED_IP_CIDR
policy="$(python3 -c "import json,sys; print(json.dumps(json.dumps(json.load(open('$(render api-resource-policy.json.tpl api-resource-policy.json)')))))")"
aws_mut apigateway update-rest-api --rest-api-id "$API_ID" --patch-operations "op=replace,path=/policy,value=$policy" >/dev/null
aws_mut apigateway create-deployment --rest-api-id "$API_ID" --stage-name "$STAGE" --description "allowlist update" >/dev/null
log "allowlist now $ALLOWED_IP_CIDR"
