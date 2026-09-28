#!/usr/bin/env bash
# Upload the six frozen NDJSON files byte-for-byte to the import bucket and prove the staged copies are identical.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
load_identity; require_state KEY_ARN IMPORT_BUCKET
( cd "$PACKAGE_DIR" && sha256sum -c SHA256SUMS.txt --quiet ) || die "frozen package failed its checksum BEFORE upload"
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
for T in "${NDJSON_TYPES[@]}"; do
  f="$PACKAGE_DIR/fhir/bulk/$T.ndjson"; [ -f "$f" ] || die "missing $f"
  local_hex="$(sha256sum "$f" | cut -d' ' -f1)"; local_b64="$(printf '%s' "$local_hex" | xxd -r -p | base64)"
  aws_mut s3api put-object --bucket "$IMPORT_BUCKET" --key "$S3_PREFIX/$T.ndjson" --body "$f" --checksum-algorithm SHA256 \
      --server-side-encryption aws:kms --ssekms-key-id "$KEY_ARN" --content-type application/x-ndjson >/dev/null
  [ "${DRY_RUN:-0}" = "1" ] && continue
  remote_b64="$(aws_ro s3api head-object --bucket "$IMPORT_BUCKET" --key "$S3_PREFIX/$T.ndjson" --checksum-mode ENABLED --query ChecksumSHA256 --output text)"
  [ "$remote_b64" = "$local_b64" ] || die "$T: S3 checksum $remote_b64 != local $local_b64"
  aws_ro s3api get-object --bucket "$IMPORT_BUCKET" --key "$S3_PREFIX/$T.ndjson" "$tmp/$T.ndjson" >/dev/null
  [ "$(sha256sum "$tmp/$T.ndjson" | cut -d' ' -f1)" = "$local_hex" ] || die "$T: downloaded copy differs from the frozen file"
  log "staged + verified $T.ndjson ($local_hex)"
done
[ "${DRY_RUN:-0}" = "1" ] && exit 0
n="$(aws_ro s3api list-objects-v2 --bucket "$IMPORT_BUCKET" --prefix "$S3_PREFIX/" --query 'length(Contents)' --output text)"
[ "$n" = "6" ] || die "import prefix holds $n objects, expected exactly 6"
( cd "$PACKAGE_DIR" && sha256sum -c SHA256SUMS.txt --quiet ) || die "frozen package changed during upload"
log "UPLOAD OK: 6 objects, checksums equal, frozen package unchanged"
