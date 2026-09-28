#!/usr/bin/env bash
# GATE C. Creates the Secrets Manager secret holding the PRODUCTION OpenAI API key (docs/adr/0011).
#
# By default, the key is read directly from the project-local, git-ignored $REPO_ROOT/.local/providers.env
# (same file/format the eval harness already uses -- see backend/app/envfile.py) -- nothing else in that file
# is read, only the OPENAI_API_KEY line. OPENAI_KEY_FILE remains an optional override for a key stored
# elsewhere; it is no longer required.
#
#   APPROVE_CREATE_OPENAI_SECRET=yes ./26_openai_secret.sh                                # reads .local/providers.env
#   APPROVE_CREATE_OPENAI_SECRET=yes OPENAI_KEY_FILE=/path/to/key ./26_openai_secret.sh   # override
#
# The key VALUE never becomes a bash variable, is never printed, and is never passed on a command line: a single
# python3 process reads the source (the override file, or the OPENAI_API_KEY line of .local/providers.env) and
# writes it directly into a fresh temp file created with `mktemp` and `chmod 600` before python ever runs, so it
# is never even briefly world/group-readable. That temp file -- never the original -- is what
# `--secret-string file://...` reads, and a `trap ... EXIT` removes it on every exit path, success or failure.
# Before use, an OPENAI_KEY_FILE override is validated to: exist outside this repository (so it can never be
# accidentally committed) and have restrictive local permissions (600 or 400 -- owner-only, no group/other
# access). Either source's key value must be exactly one non-empty, non-blank line with no retained CR (Windows
# line-ending artifacts would otherwise end up IN the secret, since `--secret-string file://...` is byte-for-byte).
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
load_identity
[ "${APPROVE_CREATE_OPENAI_SECRET:-}" = "yes" ] || die "Gate C: set APPROVE_CREATE_OPENAI_SECRET=yes only after review"

# Overridable only for tests -- real usage always resolves this from the reliably-computed $REPO_ROOT (lib.sh).
PROVIDERS_ENV_FILE="${PROVIDERS_ENV_FILE:-$REPO_ROOT/.local/providers.env}"

if [ -n "${OPENAI_KEY_FILE:-}" ]; then
  [ -f "$OPENAI_KEY_FILE" ] || die "OPENAI_KEY_FILE ($OPENAI_KEY_FILE) does not exist"
  key_real="$(realpath "$OPENAI_KEY_FILE")"
  repo_real="$(realpath "$REPO_ROOT")"
  case "$key_real" in
    "$repo_real"/*|"$repo_real") die "OPENAI_KEY_FILE ($key_real) is inside the repository ($repo_real) -- keep the key file outside any git-tracked directory" ;;
  esac
  perm="$(stat -c '%a' "$OPENAI_KEY_FILE")"
  case "$perm" in
    600|400) ;;
    *) die "OPENAI_KEY_FILE permissions are $perm; expected 600 or 400 (owner-only, no group/other access) -- run: chmod 600 $OPENAI_KEY_FILE" ;;
  esac
  KEY_SOURCE_DESC="OPENAI_KEY_FILE override"
else
  [ -f "$PROVIDERS_ENV_FILE" ] || die "no OPENAI_KEY_FILE given and $PROVIDERS_ENV_FILE does not exist -- create it (git-ignored) with a line OPENAI_API_KEY=... or set OPENAI_KEY_FILE"
  KEY_SOURCE_DESC="$PROVIDERS_ENV_FILE"
fi

tmpfile="$(mktemp)"
chmod 600 "$tmpfile"
trap 'rm -f "$tmpfile"' EXIT

python3 - "${OPENAI_KEY_FILE:-}" "$PROVIDERS_ENV_FILE" "$tmpfile" <<'PY'
import sys

override_path, providers_env_path, dest_path = sys.argv[1:4]


def stop(msg):
    print(f"STOP: {msg}", file=sys.stderr)
    raise SystemExit(1)


def one_line_key(raw: bytes, label: str) -> str:
    if b"\r" in raw:
        stop(f"{label} contains a carriage return (CRLF or a stray \\r) -- re-save it with plain LF line endings; "
             "--secret-string file://... is byte-for-byte, so a retained \\r would end up IN the secret")
    text = raw.decode("utf-8", errors="strict")
    lines = [l for l in text.split("\n") if l != ""]  # a trailing "\n" just produces one empty trailing element, dropped
    if len(lines) != 1:
        stop(f"{label} must contain exactly one non-empty line (the key), found {len(lines)}")
    if lines[0] != lines[0].strip():
        stop(f"{label}'s key line has leading/trailing whitespace")
    return lines[0]


if override_path:
    key = one_line_key(open(override_path, "rb").read(), "OPENAI_KEY_FILE")
else:
    # Mirrors backend/app/envfile.py's load_env_file() parsing rules, restricted to OPENAI_API_KEY only: nothing
    # else in .local/providers.env (ANTHROPIC_API_KEY, GEMINI_API_KEY, ...) is ever read by this script.
    key = None
    for raw_line in open(providers_env_path, "r", encoding="utf-8").read().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        name, _, value = line.partition("=")
        name, value = name.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if name == "OPENAI_API_KEY" and value:
            key = value
            break  # first match wins, same as envfile.py's load_env_file()
    if key is None:
        stop(f"OPENAI_API_KEY not found in {providers_env_path}")
    if "\r" in key or "\n" in key:
        stop(f"{providers_env_path}'s OPENAI_API_KEY value contains a stray CR/LF")
    if key != key.strip():
        stop(f"{providers_env_path}'s OPENAI_API_KEY value has leading/trailing whitespace")

with open(dest_path, "wb") as f:
    f.write(key.encode("utf-8"))
PY

expect_missing "Secrets Manager secret $OPENAI_SECRET_NAME" "ResourceNotFoundException" secretsmanager describe-secret --secret-id "$OPENAI_SECRET_NAME"
mapfile -t TAGS < <(tags_cli)
arn="$(aws_mut secretsmanager create-secret --name "$OPENAI_SECRET_NAME" \
  --description "OpenAI API key for medsafety-api (docs/adr/0011); read only by MedSafetyApiLambdaRole via a scoped GetSecretValue statement" \
  --secret-string "file://$tmpfile" --tags "${TAGS[@]}" --query ARN --output text)"
save_state OPENAI_SECRET_ARN "$arn"
log "secret created: $OPENAI_SECRET_NAME ($arn) -- value never logged, never printed, never in any state file (source: $KEY_SOURCE_DESC)"
