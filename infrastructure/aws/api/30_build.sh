#!/usr/bin/env bash
# Build the arm64 zip (fastapi, pydantic, mangum, httpx, pinned boto3, openai; app + the 7 frozen data files
# verified against SHA256SUMS). --provider openai (docs/adr/0011): bundles the openai SDK, not anthropic/
# google-genai -- see scripts/build_lambda.py's own verify_zip() for the packaged-SDK assertions.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
( cd "$REPO_ROOT/data/phase0_v1_0" && sha256sum -c SHA256SUMS.txt --quiet ) || die "frozen package failed its checksum before packaging"
"$REPO_ROOT/backend/.venv/bin/python" "$REPO_ROOT/scripts/build_lambda.py" --out "$STATE_DIR/medsafety-api.zip" --platform arm64 --provider openai
save_state ZIP_PATH "$STATE_DIR/medsafety-api.zip"; save_state ZIP_SHA256 "$(cut -d' ' -f1 "$STATE_DIR/medsafety-api.zip.sha256")"
