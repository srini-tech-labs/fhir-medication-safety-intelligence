#!/usr/bin/env bash
# READ-ONLY. Creates nothing. Confirms identity, region, name availability, quotas/existing datastores and package integrity.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
load_identity; resolve_principal
log "identity      : $IDENTITY_ARN"
log "account       : $ACCT   region: $AWS_REGION"
log "role principal: $PRINCIPAL_ARN"
aws --version >&2

log "existing HealthLake datastores (read-only; shows whether the service is reachable / not blocked by an SCP):"
aws_ro healthlake list-fhir-datastores --query 'DatastorePropertiesList[].[DatastoreName,DatastoreId,DatastoreStatus]' --output text >&2 \
  || die "healthlake:ListFHIRDatastores failed (permissions or an SCP). Attach the deployer policy or fix access; nothing was created."
existing="$(aws_ro healthlake list-fhir-datastores --query "DatastorePropertiesList[?DatastoreName=='$DS_NAME'].DatastoreId" --output text)"
[ -z "$existing" ] || log "NOTE: a datastore named $DS_NAME already exists ($existing); 20_datastore.sh will reuse it"

for b in "$IMPORT_BUCKET" "$RESULTS_BUCKET"; do
  if out="$(aws_ro s3api head-bucket --bucket "$b" 2>&1)"; then log "bucket exists (owned by this account, will reuse): $b"
  elif grep -q "404\|Not Found" <<<"$out"; then log "bucket name free: $b"
  else die "bucket $b: $out (403 = name taken by another account; choose another naming)"; fi
done
[ -n "$(aws_ro kms list-aliases --query "Aliases[?AliasName=='$KMS_ALIAS'].AliasName" --output text)" ] && log "NOTE: KMS alias $KMS_ALIAS already exists (will reuse)" || log "KMS alias free: $KMS_ALIAS"
log "CloudTrail trails (informational; no trail is created by this deployment):"
aws_ro cloudtrail describe-trails --query 'trailList[].[Name,HomeRegion]' --output text >&2 || log "cloudtrail:DescribeTrails not permitted (optional)"

log "frozen package integrity:"
( cd "$PACKAGE_DIR" && sha256sum -c SHA256SUMS.txt --quiet ) && log "package checksums OK" || die "frozen Phase 0 package does not match SHA256SUMS.txt"
n=$(cat "$PACKAGE_DIR"/fhir/bulk/*.ndjson | grep -c .); [ "$n" = "$EXPECTED_RESOURCES" ] || die "expected $EXPECTED_RESOURCES resources, found $n"
log "PREFLIGHT OK: read-only, nothing created. (An SCP that blocks HealthLake/KMS creation can only show up at create time.)"
