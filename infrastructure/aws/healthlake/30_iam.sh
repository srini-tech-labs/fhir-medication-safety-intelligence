#!/usr/bin/env bash
# IAM roles. The import role's trust names the datastore ARN, so this MUST run after 20_datastore.sh.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
load_identity; resolve_principal; require_state KEY_ARN DS_ID IMPORT_BUCKET RESULTS_BUCKET
export DS="$DS_ID" KEY_ARN IMPORT_BUCKET RESULTS_BUCKET PRINCIPAL_ARN
mapfile -t TAGS < <(tags_cli)

ensure_role() { # ensure_role NAME TRUST_TEMPLATE ACCESS_TEMPLATE POLICY_NAME
  local name="$1" trust access
  trust="$(render "$2" "$name.trust.json")"; access="$(render "$3" "$name.access.json")"
  if aws_ro iam get-role --role-name "$name" >/dev/null 2>&1; then
    aws_mut iam update-assume-role-policy --role-name "$name" --policy-document "file://$trust"
  else
    aws_mut iam create-role --role-name "$name" --assume-role-policy-document "file://$trust" --tags "${TAGS[@]}" --description "MedSafety Phase 3 ($name)"
  fi
  aws_mut iam put-role-policy --role-name "$name" --policy-name "$4" --policy-document "file://$access"
  log "role ready: $name"
}
ensure_role "$IMPORT_ROLE"   import-trust.json.tpl    import-access.json.tpl   ImportAccess
ensure_role "$APP_ROLE"      principal-trust.json.tpl app-access.json.tpl      FhirDataPlane
ensure_role "$VERIFIER_ROLE" principal-trust.json.tpl verifier-access.json.tpl VerifierAccess
save_state IMPORT_ROLE_ARN "arn:aws:iam::$ACCT:role/$IMPORT_ROLE"
save_state APP_ROLE_ARN "arn:aws:iam::$ACCT:role/$APP_ROLE"; save_state VERIFIER_ROLE_ARN "arn:aws:iam::$ACCT:role/$VERIFIER_ROLE"
save_state PRINCIPAL_ARN "$PRINCIPAL_ARN"
[ "${DRY_RUN:-0}" = "1" ] && exit 0
log "waiting 20s for IAM propagation, then checking that the deployer principal can assume the App and Verifier roles"
sleep 20
for r in "$APP_ROLE" "$VERIFIER_ROLE"; do
  aws_ro sts assume-role --role-arn "arn:aws:iam::$ACCT:role/$r" --role-session-name medsafety-check --query Credentials.Expiration --output text >/dev/null \
    || die "cannot assume $r as $PRINCIPAL_ARN (trust policy / propagation). Not widening trust automatically."
done
log "IAM OK"
