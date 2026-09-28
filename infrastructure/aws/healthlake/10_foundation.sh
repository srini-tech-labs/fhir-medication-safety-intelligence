#!/usr/bin/env bash
# KMS CMK + the two S3 buckets. Idempotent.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
load_identity; load_state

# ---- KMS -----------------------------------------------------------------------------------------------------
KEY_ID="$(aws_ro kms list-aliases --query "Aliases[?AliasName=='$KMS_ALIAS'].TargetKeyId" --output text)"
if [ -z "$KEY_ID" ]; then
  policy="$(render kms-key-policy.json.tpl kms-key-policy.json)"
  KEY_ARN="$(aws_mut kms create-key --description "MedSafety HealthLake CMK (synthetic data)" --key-usage ENCRYPT_DECRYPT --key-spec SYMMETRIC_DEFAULT \
      --policy "file://$policy" --tags TagKey=Project,TagValue=medsafety-intelligence TagKey=Phase,TagValue=3 TagKey=Environment,TagValue=dev TagKey=DataClass,TagValue=synthetic \
      --query KeyMetadata.Arn --output text)"
  KEY_ID="${KEY_ARN##*/}"
  aws_mut kms create-alias --alias-name "$KMS_ALIAS" --target-key-id "$KEY_ID"
  aws_mut kms enable-key-rotation --key-id "$KEY_ID"
else
  KEY_ARN="$(aws_ro kms describe-key --key-id "$KEY_ID" --query KeyMetadata.Arn --output text)"; log "reusing key $KEY_ARN"
fi
save_state KEY_ARN "$KEY_ARN"; save_state KEY_ID "$KEY_ID"

# ---- S3 ------------------------------------------------------------------------------------------------------
make_bucket() {
  local B="$1"
  if ! aws_ro s3api head-bucket --bucket "$B" >/dev/null 2>&1; then aws_mut s3api create-bucket --bucket "$B" --region us-east-1; fi
  aws_mut s3api put-public-access-block --bucket "$B" --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
  aws_mut s3api put-bucket-ownership-controls --bucket "$B" --ownership-controls 'Rules=[{ObjectOwnership=BucketOwnerEnforced}]'
  aws_mut s3api put-bucket-versioning --bucket "$B" --versioning-configuration Status=Enabled
  aws_mut s3api put-bucket-encryption --bucket "$B" --server-side-encryption-configuration \
    "{\"Rules\":[{\"ApplyServerSideEncryptionByDefault\":{\"SSEAlgorithm\":\"aws:kms\",\"KMSMasterKeyID\":\"$KEY_ARN\"},\"BucketKeyEnabled\":true}]}"
  BUCKET="$B" aws_mut s3api put-bucket-policy --bucket "$B" --policy "file://$(BUCKET="$B" render bucket-policy.json.tpl "bucket-policy.$B.json")"
  aws_mut s3api put-bucket-lifecycle-configuration --bucket "$B" --lifecycle-configuration "file://$HL_DIR/policies/lifecycle.json"
  aws_mut s3api put-bucket-tagging --bucket "$B" --tagging "$(tags_s3)"
  log "bucket ready: $B"
}
make_bucket "$IMPORT_BUCKET"; make_bucket "$RESULTS_BUCKET"
save_state IMPORT_BUCKET "$IMPORT_BUCKET"; save_state RESULTS_BUCKET "$RESULTS_BUCKET"
