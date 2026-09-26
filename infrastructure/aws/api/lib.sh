#!/usr/bin/env bash
# Shared helpers (same conventions as Phase 3). DRY_RUN=1 prints every mutating command instead of running it.
set -euo pipefail
# shellcheck source=env.sh
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"

log()  { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*" >&2; }
die()  { printf 'STOP: %s\n' "$*" >&2; exit 1; }
aws_ro()  { aws "$@"; }
aws_mut() { if [ "${DRY_RUN:-0}" = "1" ]; then printf 'DRY-RUN aws %s\n' "$*" >&2; return 0; fi; aws "$@"; }

save_state() { touch "$STATE_FILE"; grep -v "^$1=" "$STATE_FILE" > "$STATE_FILE.tmp" || true; printf '%s=%q\n' "$1" "$2" >> "$STATE_FILE.tmp"; mv "$STATE_FILE.tmp" "$STATE_FILE"; }
load_state() { [ -f "$STATE_FILE" ] && source "$STATE_FILE" || true; }
require_state() { load_state; for v in "$@"; do [ -n "${!v:-}" ] || die "missing $v (run the earlier step first)"; done; }

load_identity() {
  IDENTITY_ARN="$(aws_ro sts get-caller-identity --query Arn --output text)"
  ACCT="$(aws_ro sts get-caller-identity --query Account --output text)"
  [[ "$ACCT" =~ ^[0-9]{12}$ ]] || die "could not resolve a 12-digit account id"
  case "$IDENTITY_ARN" in *:root) die "refusing to run as the account root user";; esac
  export ACCT IDENTITY_ARN
}

# Phase 3 facts this phase depends on (read from its state file; the datastore itself is only ever READ here)
load_healthlake_state() {
  [ -f "$HL_STATE_FILE" ] || die "Phase 3 state not found ($HL_STATE_FILE)"
  # shellcheck disable=SC1090
  source "$HL_STATE_FILE"
  [ -n "${DS_ID:-}" ] && [ -n "${KEY_ARN:-}" ] || die "Phase 3 state lacks DS_ID / KEY_ARN"
  export DS="$DS_ID" DS_ID KEY_ARN
}

# Refuses unless DS_ID is set AND differs from the datastore id recorded (before deletion, ADR-0010) in
# datastore-recreation-record.json. Guards against the exact trap docs/adr/0010's own recreation workflow
# documents: .state/deploy.env can go on holding the DELETED datastore's id (it is only cleared by
# 91_recreate.sh, and only once recreation is actually prepared) so a naive "is DS_ID non-empty" check would
# pass against a datastore that no longer exists. Only 40_lambda.sh calls this -- Gate C, Gate D and 30_build.sh
# have no HealthLake dependency and must stay runnable while HealthLake is deleted.
require_current_healthlake_datastore() {
  local record="$REPO_ROOT/infrastructure/aws/healthlake/datastore-recreation-record.json"
  [ -f "$record" ] || die "HealthLake recreation record not found: $record"
  local deleted_id
  deleted_id="$(python3 -c "import json; print(json.load(open('$record'))['datastore']['id'])")"
  [ -n "${DS_ID:-}" ] || die "HEALTHLAKE_DATASTORE_ID is empty -- HealthLake must be recreated first (see infrastructure/aws/healthlake/91_recreate.sh)"
  [ "$DS_ID" != "$deleted_id" ] || die "HEALTHLAKE_DATASTORE_ID ($DS_ID) is the DELETED datastore recorded in datastore-recreation-record.json -- HealthLake has not been recreated yet; Gate C/D and 30_build.sh may still run, but this script refuses until recreation completes"
  export DELETED_DS_ID="$deleted_id"
}

render() { python3 "$RENDER" "$API_DIR/policies/$1" > "$STATE_DIR/rendered/$2"; printf '%s' "$STATE_DIR/rendered/$2"; }

tags_cli() { local t=(); for kv in "${TAG_KV[@]}"; do t+=("Key=${kv%%=*},Value=${kv#*=}"); done; printf '%s\n' "${t[@]}"; }
tags_kv()  { local t=(); for kv in "${TAG_KV[@]}"; do t+=("${kv%%=*}=${kv#*=}"); done; local IFS=,; printf '%s' "${t[*]}"; }

# expect_missing DESCRIPTION ERROR_REGEX aws-args...   -> dies if the resource EXISTS or if absence cannot be confirmed
expect_missing() {
  local desc="$1" pat="$2"; shift 2
  local out
  if out="$(aws "$@" 2>&1)"; then die "unexpected existing $desc"; fi
  grep -qE "$pat" <<<"$out" || die "cannot confirm $desc is absent: $(head -c 300 <<<"$out")"
}
