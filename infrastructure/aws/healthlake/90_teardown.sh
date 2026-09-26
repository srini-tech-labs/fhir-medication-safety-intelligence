#!/usr/bin/env bash
# DESTRUCTIVE. Deletes the datastore, roles, buckets (all versions) and schedules key deletion. Never run automatically.
#   CONFIRM_TEARDOWN=yes ./90_teardown.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
load_identity; load_state
[ "${CONFIRM_TEARDOWN:-}" = "yes" ] || die "set CONFIRM_TEARDOWN=yes to delete the Phase 3 AWS resources (the user must approve this first)"
if [ -n "${DS_ID:-}" ]; then
  aws_mut healthlake delete-fhir-datastore --datastore-id "$DS_ID"
  if [ "${DRY_RUN:-0}" != "1" ]; then
    until ! out="$(aws_ro healthlake describe-fhir-datastore --datastore-id "$DS_ID" --query DatastoreProperties.DatastoreStatus --output text 2>&1)" || [ "$out" = DELETED ]; do
      log "datastore: $out"; sleep 30; done
  fi
fi
# HealthLake's analytics integration creates a Glue resource-link database in THIS account (observed live:
# medsafety_fhir_r4_<datastoreId>_healthlake_view). Remove it if the datastore deletion left it behind; leave every other database alone.
if [ -n "${DS_ID:-}" ]; then
  GLUE_DB="medsafety_fhir_r4_${DS_ID}_healthlake_view"
  if aws_ro glue get-database --name "$GLUE_DB" >/dev/null 2>&1; then aws_mut glue delete-database --name "$GLUE_DB"; else log "no leftover Glue database $GLUE_DB"; fi
fi
for R in "$IMPORT_ROLE" "$APP_ROLE" "$VERIFIER_ROLE"; do
  aws_ro iam get-role --role-name "$R" >/dev/null 2>&1 || continue
  for P in $(aws_ro iam list-role-policies --role-name "$R" --query PolicyNames --output text); do aws_mut iam delete-role-policy --role-name "$R" --policy-name "$P"; done
  aws_mut iam delete-role --role-name "$R"
done
for B in "$IMPORT_BUCKET" "$RESULTS_BUCKET"; do
  aws_ro s3api head-bucket --bucket "$B" >/dev/null 2>&1 || continue
  while :; do
    objs="$(aws_ro s3api list-object-versions --bucket "$B" --max-items 500 --query '{Objects: [Versions, DeleteMarkers][][].{Key:Key,VersionId:VersionId}}' --output json)"
    [ "$(python3 -c "import json,sys;print(len(json.load(sys.stdin).get('Objects') or []))" <<<"$objs")" = 0 ] && break
    [ "${DRY_RUN:-0}" = "1" ] && break
    aws s3api delete-objects --bucket "$B" --delete "$objs" >/dev/null
  done
  aws_mut s3api delete-bucket --bucket "$B"
done
if [ -n "${KEY_ID:-}" ]; then
  aws_mut kms delete-alias --alias-name "$KMS_ALIAS" || true
  aws_mut kms schedule-key-deletion --key-id "$KEY_ID" --pending-window-in-days 7
fi
log "teardown finished (KMS key deletes after the 7-day minimum window)"
