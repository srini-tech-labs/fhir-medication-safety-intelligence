#!/usr/bin/env bash
# Native HealthLake bulk import with STRICT validation. Stops (non-zero) on anything but COMPLETED with 53/53/0.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
load_identity; require_state KEY_ARN DS_ID IMPORT_BUCKET RESULTS_BUCKET IMPORT_ROLE_ARN
# Always resolve the job id live, scoped to the CURRENT $DS_ID -- never fall back to a JOB_ID left in the state file. A
# state file can carry a JOB_ID from a datastore that was since deleted and recreated (a new datastore never reuses the
# old job's id); trusting it here would describe the wrong datastore's job and fail. list-fhir-import-jobs is cheap and
# already scoped by --datastore-id, so there is no reason to cache it.
JOB_ID="$(aws_ro healthlake list-fhir-import-jobs --datastore-id "$DS_ID" --query "ImportJobPropertiesList[?JobName=='$JOB_NAME'].JobId | [0]" --output text)"
[ "$JOB_ID" = "None" ] && JOB_ID=""
if [ -z "$JOB_ID" ]; then
  JOB_ID="$(aws_mut healthlake start-fhir-import-job --datastore-id "$DS_ID" --job-name "$JOB_NAME" \
     --input-data-config "S3Uri=s3://$IMPORT_BUCKET/$S3_PREFIX/" \
     --job-output-data-config "S3Configuration={S3Uri=s3://$RESULTS_BUCKET/$S3_PREFIX/,KmsKeyId=$KEY_ARN}" \
     --data-access-role-arn "$IMPORT_ROLE_ARN" --validation-level strict --client-token "$JOB_CLIENT_TOKEN" --query JobId --output text)"
fi
[ "${DRY_RUN:-0}" = "1" ] && exit 0
save_state JOB_ID "$JOB_ID"; log "import job $JOB_ID (validation-level strict)"
while :; do
  status="$(aws_ro healthlake describe-fhir-import-job --datastore-id "$DS_ID" --job-id "$JOB_ID" --query ImportJobProperties.JobStatus --output text)"
  log "job status: $status"; case "$status" in COMPLETED|COMPLETED_WITH_ERRORS|FAILED|CANCELLED) break;; esac; sleep 15
done
aws_ro healthlake describe-fhir-import-job --datastore-id "$DS_ID" --job-id "$JOB_ID" --output json > "$STATE_DIR/import-job.json"
# AWS names the file `Manifest.json` (capital M); match case-insensitively for this job's folder only.
manifest_key="$(aws_ro s3api list-objects-v2 --bucket "$RESULTS_BUCKET" --prefix "$S3_PREFIX/" --query "Contents[].Key" --output json \
  | python3 -c "import json,sys; k=[x for x in json.load(sys.stdin) or [] if x.lower().endswith('/manifest.json') and '$JOB_ID' in x]; print(k[0] if k else 'None')")"
if [ "$manifest_key" != "None" ] && [ -n "$manifest_key" ]; then
  aws_ro s3 cp "s3://$RESULTS_BUCKET/$manifest_key" "$STATE_DIR/import-manifest.json" >/dev/null
  cat "$STATE_DIR/import-manifest.json" >&2; echo >&2
fi
if [ "$status" != COMPLETED ]; then
  log "results listing:"; aws_ro s3 ls "s3://$RESULTS_BUCKET/$S3_PREFIX/" --recursive >&2 || true
  die "import finished as $status. NOT retrying, NOT loosening validation, NOT editing the frozen files. Report the FAILURE/ files."
fi
python3 - "$STATE_DIR/import-manifest.json" "$EXPECTED_RESOURCES" <<'PY' || die "manifest counts differ from the expected 53/53/0 (or the manifest is unreadable)"
import json, sys
m = json.load(open(sys.argv[1])); exp = int(sys.argv[2])
def pick(*needles):  # RESOURCE counts only (numberOfResources*), never numberOfScannedFILES or the successOutput/
    # failureOutput S3-uri objects -- a loose "contains" match across ALL top-level keys previously picked
    # numberOfScannedFiles (files, not resources) for "scanned", and the successOutput dict (an S3 URI holder,
    # not a count) for "success", crashing on int(dict). Restricting to the numberOfResources* prefix and
    # skipping non-scalar values makes the match unambiguous.
    for k, v in m.items():
        lk = k.lower()
        if lk.startswith("numberofresources") and not isinstance(v, dict) and all(n in lk for n in needles):
            return int(v or 0)
    raise SystemExit(f"manifest has no numberOfResources* key containing {needles}: {sorted(k for k in m if 'numberofresources' in k.lower())}")
got = (pick("scanned"), pick("success"), pick("customer"), pick("server"))
print("scanned/imported/customerError/serverError =", got)
sys.exit(0 if got == (exp, exp, 0, 0) else 1)
PY
log "IMPORT OK: 53 scanned, 53 imported, 0 errors"
