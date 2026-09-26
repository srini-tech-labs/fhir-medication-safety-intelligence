#!/usr/bin/env bash
# Sourced by every script. Names are fixed by the approved Phase 3 deployment plan. No credentials live in any file:
# the operator configures a named AWS profile (`aws configure sso` / `aws configure --profile ...`) and exports AWS_PROFILE.
: "${AWS_PROFILE:?Set AWS_PROFILE to the profile you configured for this deployment (never put keys in files or chat)}"
export AWS_REGION=us-east-1 AWS_DEFAULT_REGION=us-east-1 AWS_PAGER=""
[ "$AWS_REGION" = "us-east-1" ] || { echo "region must be us-east-1" >&2; exit 1; }

HL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HL_DIR/../../.." && pwd)"
PACKAGE_DIR="$REPO_ROOT/data/phase0_v1_0"
STATE_DIR="${STATE_DIR:-$HL_DIR/.state}"          # gitignored: ids, ARNs, rendered policies (no secrets)
STATE_FILE="$STATE_DIR/deploy.env"
mkdir -p "$STATE_DIR/rendered"

KMS_ALIAS="alias/medsafety-healthlake-cmk"
DS_NAME="medsafety-fhir-r4"
# Overridable (not plain assignment): a datastore recreation after deletion must use a NEW client token -- AWS's
# idempotency window for a token is tied to the original request/resource, and reusing one bound to a deleted
# datastore is unproven and untested. 91_recreate.sh exports fresh tokens; a first/normal deployment is unaffected.
DS_CLIENT_TOKEN="${DS_CLIENT_TOKEN:-medsafety-phase3-ds-001}"
IMPORT_ROLE="MedSafetyHealthLakeImportRole"
APP_ROLE="MedSafetyHealthLakeAppRole"
VERIFIER_ROLE="MedSafetyHealthLakeVerifierRole"
JOB_NAME="medsafety-phase0-v1-import"
JOB_CLIENT_TOKEN="${JOB_CLIENT_TOKEN:-medsafety-phase0-v1-import-001}"
S3_PREFIX="phase0-v1"
NDJSON_TYPES=(Patient Encounter MedicationRequest Observation DocumentReference Binary)
EXPECTED_RESOURCES=53
TAG_KV=(Project=medsafety-intelligence Phase=3 Environment=dev DataClass=synthetic)
