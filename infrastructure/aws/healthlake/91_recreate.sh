#!/usr/bin/env bash
# Safe HealthLake recreation workflow, built from datastore-recreation-record.json (written before the 2026-09-19 deletion).
#
# This script NEVER calls create-fhir-datastore, start-fhir-import-job, put-role-policy, update-function-configuration, or
# any other mutating API. It only:
#   1. confirms (read-only) that "medsafety-fhir-r4" is truly gone -- never ACTIVE/CREATING -- so nothing double-creates
#   2. confirms the dependencies this recreation reuses are exactly as recorded: the CMK enabled with its alias, the three
#      Phase 3 IAM roles present, and both S3 buckets holding the same object counts as when the record was written
#   3. refuses to proceed with either client token recorded for the DELETED datastore/job (an idempotency token's
#      guarantee is tied to the resource it created; reusing one after that resource is gone is unproven and untested --
#      a fresh token is required for both create-fhir-datastore and start-fhir-import-job)
#   4. backs up, then clears, the identifiers in .state/deploy.env that named the deleted datastore (DS_ID, DS_ARN,
#      DS_ENDPOINT, DS_CREATED_AT, JOB_ID) -- 50_import.sh has a fallback that reuses a JOB_ID left in state without
#      checking it belongs to the current datastore; a stale one there would target the wrong datastore's job
#   5. prints the exact ordered commands to run BY HAND -- each is reviewed and approved separately; this script performs
#      none of them
#
# Usage (all four must be supplied; there is no default):
#   RECREATE_DS_CLIENT_TOKEN=<new> RECREATE_JOB_CLIENT_TOKEN=<new> CONFIRM_RECREATE=yes ./91_recreate.sh
#
# DRY_RUN=1 runs every check but does not touch .state/deploy.env (prints what it would clear instead).
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
[ "${CONFIRM_RECREATE:-}" = "yes" ] || die "set CONFIRM_RECREATE=yes to prepare a HealthLake recreation (the user must approve this first); this script itself creates nothing"
RECORD="${RECREATE_RECORD:-$HL_DIR/datastore-recreation-record.json}"
[ -f "$RECORD" ] || die "recreation record not found: $RECORD"
load_identity

record_get() { python3 -c "import json,sys; d=json.load(open(sys.argv[1]))
for k in sys.argv[2].split('.'): d = d[k]
print(d)" "$RECORD" "$1"; }

for k in kms.keyArn kms.alias importJob.dataAccessRoleArn datastore.id; do record_get "$k" >/dev/null || die "recreation record missing $k"; done
log "record OK: recorded datastore $(record_get datastore.id) ($(record_get datastore.fhirVersion), $(record_get datastore.authorizationStrategy))"

# ---- 1. the named datastore must be gone (DELETED or never existed) --------------------------------------------
existing="$(aws_ro healthlake list-fhir-datastores --query "DatastorePropertiesList[?DatastoreName=='$DS_NAME' && DatastoreStatus!='DELETED'].[DatastoreId,DatastoreStatus]" --output text)"
[ -z "$existing" ] || die "a datastore named $DS_NAME already exists and is not DELETED ($existing) -- refusing to prepare a recreation while one is live"
log "confirmed: no live (non-DELETED) datastore named $DS_NAME"

# ---- 2. dependencies this recreation reuses must be exactly as recorded ----------------------------------------
KEY_ARN_REC="$(record_get kms.keyArn)"; KEY_ALIAS_REC="$(record_get kms.alias)"
read -r key_state key_enabled <<<"$(aws_ro kms describe-key --key-id "$KEY_ARN_REC" --query 'KeyMetadata.[KeyState,Enabled]' --output text)"
[ "$key_state" = "Enabled" ] && [ "$key_enabled" = "True" ] || die "CMK $KEY_ARN_REC is not Enabled ($key_state/$key_enabled) -- cannot recreate encrypted with a disabled/deleted key"
aws_ro kms list-aliases --key-id "$KEY_ARN_REC" --query "Aliases[?AliasName=='$KEY_ALIAS_REC']" --output text | grep -q "$KEY_ALIAS_REC" \
  || die "alias $KEY_ALIAS_REC no longer points at $KEY_ARN_REC"
for role in "$IMPORT_ROLE" "$APP_ROLE" "$VERIFIER_ROLE"; do
  aws_ro iam get-role --role-name "$role" >/dev/null 2>&1 || die "IAM role $role is missing; the record assumed it still exists -- report before continuing"
done
while read -r bucket recorded; do
  [ -n "$bucket" ] || continue
  aws_ro s3api head-bucket --bucket "$bucket" >/dev/null 2>&1 || die "bucket $bucket from the record no longer exists"
  now="$(aws_ro s3api list-objects-v2 --bucket "$bucket" --query 'length(Contents)' --output text)"
  [ "$now" = "$recorded" ] || die "bucket $bucket now has $now objects, the record has $recorded -- investigate before re-importing from it"
done < <(python3 -c "import json; r=json.load(open('$RECORD'))
for b, objs in r['s3ObjectsAtRecordTime'].items(): print(b, len(objs))")
log "dependencies OK: CMK enabled with its alias, 3 IAM roles present, S3 object counts match the record"

# ---- 3. refuse the OLD client tokens; require fresh ones --------------------------------------------------------
old_ds_token="$(record_get datastore.createClientToken | awk '{print $1}')"
old_job_token="$(record_get importJob.clientToken | awk '{print $1}')"
new_ds_token="${RECREATE_DS_CLIENT_TOKEN:?set RECREATE_DS_CLIENT_TOKEN to a NEW, never-used client token for create-fhir-datastore}"
new_job_token="${RECREATE_JOB_CLIENT_TOKEN:?set RECREATE_JOB_CLIENT_TOKEN to a NEW, never-used client token for start-fhir-import-job}"
[ "$new_ds_token" != "$old_ds_token" ] || die "RECREATE_DS_CLIENT_TOKEN reuses the token bound to the deleted datastore ($old_ds_token) -- choose a new one"
[ "$new_job_token" != "$old_job_token" ] || die "RECREATE_JOB_CLIENT_TOKEN reuses the token bound to the deleted datastore's import job ($old_job_token) -- choose a new one"
log "client tokens are new (neither equals the one recorded for the deleted datastore/job)"

# ---- 4. back up, then clear, the identifiers that belonged to the deleted datastore -----------------------------
ts="$(date -u +%Y%m%dT%H%M%SZ)"
STALE_KEYS=(DS_ID DS_ARN DS_ENDPOINT DS_CREATED_AT JOB_ID)
if [ "${DRY_RUN:-0}" = "1" ]; then
  log "DRY_RUN: would back up $STATE_FILE and clear ${STALE_KEYS[*]} from it"
else
  if [ -f "$STATE_FILE" ]; then
    backup="$STATE_DIR/deploy.env.pre-recreate-$ts"
    cp "$STATE_FILE" "$backup"
    for k in "${STALE_KEYS[@]}"; do sed -i "/^$k=/d" "$STATE_FILE"; done
    log "backed up state to $backup; cleared ${STALE_KEYS[*]} from $STATE_FILE"
  else
    log "no existing $STATE_FILE to clear"
  fi
fi

# ---- 5. print the plan; this script stops here, it runs nothing mutating ----------------------------------------
cat <<PLAN

Recreation is PREPARED, not started. Nothing has been created. Run these BY HAND, in order, reviewing each before
the next (each script is idempotent and safe to re-run if a later step is aborted):

  1. DS_CLIENT_TOKEN=$new_ds_token bash "$HL_DIR/20_datastore.sh"
       -> creates a NEW datastore (new id and endpoint). Same CMK, FHIR R4, AWS_AUTH, analytics PAUSED, same tags.
  2. bash "$HL_DIR/30_iam.sh"
       -> re-renders and re-applies the Import/App/Verifier role trust + access policies against the NEW datastore ARN
  3. JOB_CLIENT_TOKEN=$new_job_token bash "$HL_DIR/50_import.sh"
       -> re-imports the SAME frozen S3 objects under --validation-level strict; stops unless 53/53/0 COMPLETED
  4. backend/.venv/bin/python scripts/healthlake_verify.py --datastore-id <new-id> \\
       --verifier-role-arn \$VERIFIER_ROLE_ARN --app-role-arn \$APP_ROLE_ARN --checks V0,V1,V2,V3,V4,V5 --out /tmp/hl-quick.json
       -> SHORTENED parity check: capability statement, one read, identifier search, counts, patient-scoped search,
          Binary note match -- a fast go/no-go before the full run
  5. backend/.venv/bin/python scripts/healthlake_verify.py --datastore-id <new-id> \\
       --verifier-role-arn \$VERIFIER_ROLE_ARN --app-role-arn \$APP_ROLE_ARN --out infrastructure/aws/healthlake/.state/verification-report.json
       -> full V0-V19
  6. bash infrastructure/aws/api/20_role.sh    # Phase 4 Lambda role: BaseAccess re-rendered with the NEW datastore ARN
  7. bash infrastructure/aws/api/40_lambda.sh  # updates the Lambda's HEALTHLAKE_DATASTORE_ID env var only (no rebuild)
  8. backend/.venv/bin/python scripts/api_verify.py --mode lambda --checks A2,A3,A4,A5,A9,A14 ...
       -> shortened API-level parity against the new datastore, before API Gateway traffic resumes
  9. bash infrastructure/aws/api/60_verify.sh  # full A0-A16 over HTTPS; A7/A8 stay --pending (Bedrock is untouched)

None of the above ran. Re-run this script (safe, idempotent) if you want the pre-checks re-verified before
continuing by hand.
PLAN
