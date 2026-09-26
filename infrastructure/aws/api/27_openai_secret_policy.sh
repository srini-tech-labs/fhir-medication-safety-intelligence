#!/usr/bin/env bash
# GATE D. Adds least-privilege `secretsmanager:GetSecretValue` -- scoped to EXACTLY the one OpenAI secret ARN,
# nothing else -- to the Lambda role. Refuses to run unless the operator approved the exact statement printed
# by this script:   APPROVE_OPENAI_SECRET_POLICY=yes ./27_openai_secret_policy.sh
# Fails closed if an inline policy named OpenAiSecretRead already exists on the role -- `put-role-policy` is an
# overwrite-if-exists AWS API, so without this check a second run (or a name collision with something else) would
# silently replace an existing statement instead of stopping for review.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
load_identity; require_state OPENAI_SECRET_ARN
expect_missing "inline policy OpenAiSecretRead on $ROLE" "NoSuchEntity" iam get-role-policy --role-name "$ROLE" --policy-name OpenAiSecretRead
export OPENAI_SECRET_ARN
statements="$(render lambda-secrets-openai.json.tpl lambda-secrets-openai.json)"
cat "$statements" >&2; echo >&2
[ "${APPROVE_OPENAI_SECRET_POLICY:-}" = "yes" ] || die "Gate D: set APPROVE_OPENAI_SECRET_POLICY=yes only after the operator approved the statement above"
aws_mut iam put-role-policy --role-name "$ROLE" --policy-name OpenAiSecretRead --policy-document "file://$statements"
log "OpenAI secret-read statement applied to $ROLE (scoped to exactly one secret ARN: $OPENAI_SECRET_ARN)"
