#!/usr/bin/env bash
# READ-ONLY identity check of the project-local AWS setup. Makes ONLY `sts get-caller-identity` calls: it creates, changes and
# deploys nothing. Never prints credentials.
#
#   scripts/aws_identity_check.sh            (run from anywhere; it sources .env.aws from the project root)
#
# Verifies: (1) the bootstrap access key works and is the expected IAM user, (2) that user can AssumeRole into the deployer role
# (which proves the role's trust policy), (3) the project-local config/region, (4) the credential files are ignored by Git.
set -euo pipefail

ROOT="${PROJECT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
ACCT="123456789012"
EXPECT_BOOTSTRAP="arn:aws:iam::$ACCT:user/medsafety-bootstrap"
EXPECT_ROLE_PREFIX="arn:aws:sts::$ACCT:assumed-role/MedSafetyPhase3DeployerRole/"
EXPECT_SESSION="medsafety-phase3"

fail() { printf 'STOP: %s\n' "$*" >&2; exit 1; }
ok()   { printf 'OK: %s\n' "$*"; }

cd "$ROOT"
[ -f .env.aws ] || fail ".env.aws not found in $ROOT"
# Ambient credentials would silently bypass the project-local files (and could reach other AWS resources): refuse them.
for v in AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN; do
  [ -z "${!v:-}" ] || fail "$v is set in the environment; unset it so only the project-local profiles are used"
done
# shellcheck disable=SC1091
source ./.env.aws
[ "$AWS_CONFIG_FILE" = "$ROOT/.aws-local/config" ] && [ "$AWS_SHARED_CREDENTIALS_FILE" = "$ROOT/.aws-local/credentials" ] \
  || fail "AWS_CONFIG_FILE / AWS_SHARED_CREDENTIALS_FILE do not point at $ROOT/.aws-local (source .env.aws from the project root)"
[ -f "$AWS_CONFIG_FILE" ] && [ -f "$AWS_SHARED_CREDENTIALS_FILE" ] || fail "missing .aws-local/config or .aws-local/credentials"

# Placeholders: stop BEFORE any AWS call. (grep -q prints nothing, so no credential content can leak.)
if grep -q 'REPLACE_LOCALLY' "$AWS_SHARED_CREDENTIALS_FILE"; then
  printf '%s\n' "Project-local AWS configuration is ready. Add the bootstrap Access Key ID and Secret Access Key directly to .aws-local/credentials, then tell me to continue."
  exit 3
fi

# Git safety: the credential files must be ignored and untracked.
for f in .aws-local/credentials .aws-local/config .env.aws; do
  git check-ignore -q "$f" || fail "$f is NOT ignored by git"
done
[ -z "$(git ls-files .aws-local .env.aws)" ] || fail "credential/config files are tracked by git"
ok "credential files are ignored by git and not tracked"

# Region/config
[ "$(aws configure get region --profile medsafety)" = "us-east-1" ] && [ "$(aws configure get region --profile medsafety-bootstrap)" = "us-east-1" ] \
  || fail "profile region is not us-east-1"
[ "$(aws configure get role_arn --profile medsafety)" = "arn:aws:iam::$ACCT:role/MedSafetyPhase3DeployerRole" ] || fail "profile medsafety role_arn mismatch"
[ "$(aws configure get source_profile --profile medsafety)" = "medsafety-bootstrap" ] || fail "profile medsafety source_profile mismatch"
ok "region us-east-1 and profile configuration verified"

# 1. bootstrap identity
if ! out="$(AWS_PROFILE=medsafety-bootstrap aws sts get-caller-identity --query '[Account,Arn]' --output text 2>&1)"; then
  fail "bootstrap identity check failed: $(head -c 300 <<<"$out" | tr '\n' ' ')"
fi
read -r acct arn <<<"$out"
[ "$acct" = "$ACCT" ] || fail "bootstrap account is $acct, expected $ACCT"
[ "$arn" = "$EXPECT_BOOTSTRAP" ] || fail "bootstrap identity is $arn, expected $EXPECT_BOOTSTRAP"
ok "bootstrap identity verified ($arn); account $ACCT verified"

# 2. role assumption via the project profile
if ! out="$(AWS_PROFILE=medsafety aws sts get-caller-identity --query '[Account,Arn]' --output text 2>&1)"; then
  fail "deployer-role assumption FAILED (not changing IAM policies or trust): $(head -c 400 <<<"$out" | tr '\n' ' ')"
fi
read -r acct arn <<<"$out"
[ "$acct" = "$ACCT" ] || fail "role account is $acct, expected $ACCT"
[[ "$arn" == "$EXPECT_ROLE_PREFIX$EXPECT_SESSION" ]] || fail "role identity is $arn, expected ${EXPECT_ROLE_PREFIX}${EXPECT_SESSION}"
ok "deployer-role assumption verified ($arn)"
ok "identity verification complete: no AWS resource or permission was created or changed"
