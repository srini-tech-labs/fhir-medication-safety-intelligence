#!/usr/bin/env bash
# Shared helpers. DRY_RUN=1 prints every mutating command instead of running it; read-only calls always run.
set -euo pipefail
# shellcheck source=env.sh
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"

log()  { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*" >&2; }
die()  { printf 'STOP: %s\n' "$*" >&2; exit 1; }

# read-only AWS call (always executed)
aws_ro() { aws "$@"; }
# mutating AWS call (skipped under DRY_RUN=1)
aws_mut() {
  if [ "${DRY_RUN:-0}" = "1" ]; then printf 'DRY-RUN aws %s\n' "$*" >&2; return 0; fi
  aws "$@"
}

save_state() { # save_state KEY VALUE  (idempotent upsert into deploy.env)
  touch "$STATE_FILE"; grep -v "^$1=" "$STATE_FILE" > "$STATE_FILE.tmp" || true
  printf '%s=%q\n' "$1" "$2" >> "$STATE_FILE.tmp"; mv "$STATE_FILE.tmp" "$STATE_FILE"
}
load_state() { [ -f "$STATE_FILE" ] && source "$STATE_FILE" || true; }

load_identity() {
  IDENTITY_ARN="$(aws_ro sts get-caller-identity --query Arn --output text)"
  ACCT="$(aws_ro sts get-caller-identity --query Account --output text)"
  [[ "$ACCT" =~ ^[0-9]{12}$ ]] || die "could not resolve a 12-digit account id"
  case "$IDENTITY_ARN" in *:root) die "refusing to run as the account root user ($IDENTITY_ARN); use an IAM/SSO identity";; esac
  IMPORT_BUCKET="medsafety-hl-import-$ACCT-us-east-1"
  RESULTS_BUCKET="medsafety-hl-results-$ACCT-us-east-1"
  export ACCT IDENTITY_ARN IMPORT_BUCKET RESULTS_BUCKET
}

# The long-term principal to trust in the App/Verifier roles. An assumed-role session ARN
# (arn:aws:sts::ACCT:assumed-role/NAME/SESSION) is NOT usable: it changes every session, so use the underlying IAM role.
resolve_principal() {
  if [ -n "${PRINCIPAL_ARN:-}" ]; then
    [[ "$PRINCIPAL_ARN" =~ ^arn:aws:iam::$ACCT:(role|user)/.+ ]] || die "PRINCIPAL_ARN must be an IAM role or user ARN in account $ACCT"
  else
    case "$IDENTITY_ARN" in
      arn:aws:iam::*:user/*) PRINCIPAL_ARN="$IDENTITY_ARN";;
      arn:aws:sts::*:assumed-role/*/*)
        local role_name; role_name="$(cut -d/ -f2 <<<"$IDENTITY_ARN")"
        PRINCIPAL_ARN="$(aws_ro iam get-role --role-name "$role_name" --query Role.Arn --output text)" \
          || die "could not resolve the IAM role behind $IDENTITY_ARN; set PRINCIPAL_ARN=<role ARN> and re-run";;
      *) die "unsupported identity type: $IDENTITY_ARN (set PRINCIPAL_ARN explicitly)";;
    esac
  fi
  case "$PRINCIPAL_ARN" in *:assumed-role/*|*:root) die "principal must be a role or user, not a session/root: $PRINCIPAL_ARN";; esac
  export PRINCIPAL_ARN
}

render() { # render TEMPLATE_NAME OUT_NAME  -> path
  local out="$STATE_DIR/rendered/$2"
  python3 "$HL_DIR/render_policy.py" "$HL_DIR/policies/$1" > "$out"; printf '%s' "$out"
}

tags_s3()  { local t=""; for kv in "${TAG_KV[@]}"; do t+="{Key=${kv%%=*},Value=${kv#*=}},"; done; printf 'TagSet=[%s]' "${t%,}"; }
tags_cli() { local t=(); for kv in "${TAG_KV[@]}"; do t+=("Key=${kv%%=*},Value=${kv#*=}"); done; printf '%s\n' "${t[@]}"; }

require_state() { load_state; for v in "$@"; do [ -n "${!v:-}" ] || die "missing $v (run the earlier step first)"; done; }
