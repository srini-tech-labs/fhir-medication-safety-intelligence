#!/usr/bin/env bash
# Sourced by every Phase 4 script. Names are fixed by the approved Phase 4 plan (Revision 2). No credentials in any file.
: "${AWS_PROFILE:?Set AWS_PROFILE (source .env.aws from the project root first)}"
export AWS_REGION=us-east-1 AWS_DEFAULT_REGION=us-east-1 AWS_PAGER=""

API_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$API_DIR/../../.." && pwd)"
STATE_DIR="${STATE_DIR:-$API_DIR/.state}"                 # gitignored: ids, ARNs, rendered policies, the zip (no secrets)
STATE_FILE="$STATE_DIR/deploy.env"
HL_STATE_FILE="${HL_STATE_FILE:-$REPO_ROOT/infrastructure/aws/healthlake/.state/deploy.env}"   # Phase 3 state (datastore id, CMK arn)
RENDER="$REPO_ROOT/infrastructure/aws/healthlake/render_policy.py"
mkdir -p "$STATE_DIR/rendered"

TABLE="medsafety-app-state"
LOG_GROUP="/aws/lambda/medsafety-api"
ROLE="MedSafetyApiLambdaRole"
FUNCTION="medsafety-api"
API_NAME="medsafety-api"
STAGE="dev"
BASE_MODEL_ID="amazon.nova-2-lite-v1:0"
MODEL_ID="us.amazon.nova-2-lite-v1:0"                       # US geo inference profile, called from us-east-1 (Bedrock/Nova path: pending AWS Support case)
OPENAI_MODEL_ID="gpt-5.6-luna"                               # docs/adr/0011: the initial DEPLOYED cloud explanation provider
OPENAI_SECRET_NAME="medsafety/openai-api-key"                # Secrets Manager; the key VALUE is never in this repo or any state file
TAG_KV=(Project=medsafety-intelligence Phase=4 Environment=dev DataClass=synthetic)
