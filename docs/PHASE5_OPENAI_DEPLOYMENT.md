# Phase 5 — deploy OpenAI (`gpt-5.6-luna`) as the explanation provider

**Status: executed and live-verified, 2026-09-23.** Every step below ran for real, in the order shown, each
reviewed via `DRY_RUN=1` first. Secret: `arn:aws:secretsmanager:us-east-1:123456789012:secret:medsafety/openai-api-key-AbCdEf`.
Lambda: deployed, `LastUpdateStatus: Successful`, env vars read back and confirmed exact. A real OpenAI
explanation call passed end to end (`mode: llm`, `model: gpt-5.6-luna`, `groundedInFindingsOnly: true`) through
both direct Lambda invoke and the live HTTPS API Gateway path. See ADR-0011 for the "why"; the application
architecture itself did not change — only configuration and one new AWS resource type (Secrets Manager). This
document is kept as the exact command reference for any future redeploy or rollback.

> **Note (later the same day, 2026-09-23):** this document predates the explicit FHIR write-back feature —
> `HEALTHLAKE_WRITE_OUTPUTS: "false"` below reflects the OpenAI-deployment run only, not current state. Write-back
> was designed, implemented and deployed live afterward; see `docs/CHANGE_LOG.md`'s "Explicit FHIR write-back
> deployed live" entry and `docs/ARCHITECTURE.md` for the current, accurate picture.

## Preconditions and execution ordering

- HealthLake is currently **deleted** (ADR-0010) — its recreation is intentionally **not** part of this plan and
  is a separate, by-hand decision (`infrastructure/aws/healthlake/91_recreate.sh`).
- Execution is split by HealthLake dependency, not run as one all-or-nothing sequence:
  - **Gate C** (`26_openai_secret.sh`), **Gate D** (`27_openai_secret_policy.sh`), and the **build**
    (`30_build.sh`) have no HealthLake dependency at all and may run — and be reviewed/approved — while
    HealthLake stays deleted.
  - **`40_lambda.sh` refuses to run** until a HealthLake datastore actually exists: it now calls
    `require_current_healthlake_datastore` (`infrastructure/aws/api/lib.sh`) immediately after loading Phase 3
    state, which dies if `HEALTHLAKE_DATASTORE_ID` is empty *or* equals the id recorded (before deletion) in
    `infrastructure/aws/healthlake/datastore-recreation-record.json`. This closes a real trap: `.state/deploy.env`
    keeps holding the deleted datastore's id until `91_recreate.sh` explicitly clears it, so a naive
    "is it set" check would have passed against a datastore that no longer exists.
- `MedSafetyApiLambdaRole` and DynamoDB `medsafety-app-state` are already deployed from Phase 4 and are reused,
  not recreated.
- A production OpenAI API key is available as `OPENAI_API_KEY` in the project-local, git-ignored
  `.local/providers.env` (the same file/format the eval harness already uses — never committed, never in shell
  history — see Gate C), or in an out-of-repo file for the `OPENAI_KEY_FILE` override.

## Gate C — create the secret

```
aws secretsmanager create-secret \
  --name medsafety/openai-api-key \
  --description "OpenAI API key for medsafety-api (docs/adr/0011); read only by MedSafetyApiLambdaRole via a scoped GetSecretValue statement" \
  --secret-string file:///tmp/<a fresh mode-600 temp file holding only the key, written by 26_openai_secret.sh> \
  --tags Key=project,Value=medsafety Key=phase,Value=5 \
  --query ARN --output text
```
Run via `APPROVE_CREATE_OPENAI_SECRET=yes ./26_openai_secret.sh`. By default the key is read from
`OPENAI_API_KEY` in `.local/providers.env` (only that one line — nothing else in the file is read); set
`OPENAI_KEY_FILE=/path/to/local/key-only-file` to read from a different location instead (optional override, no
longer required). Either way, the key value **never becomes a bash variable and is never printed**: a single
`python3` process reads the source and writes it directly into a fresh temp file (`mktemp` immediately followed
by `chmod 600`, before python ever runs), and only that temp file — never the original — is what
`--secret-string file://...` reads. A `trap ... EXIT` removes the temp file on every exit path, success or
failure, so nothing is ever left behind even if the script later dies (e.g. the secret already exists). The AWS
CLI's `file://` mechanism additionally keeps the value out of the process list and shell history the whole way
through. Before touching AWS at all, an `OPENAI_KEY_FILE` override is validated to be:
- **not inside this repository** (`realpath` comparison against `REPO_ROOT` — a key file placed in-repo could
  otherwise be accidentally committed);
- **restrictive local permissions** — exactly `600` or `400` (owner-only, no group/other access); anything else
  is refused with the `chmod` command to fix it.

Either source's key value must be **exactly one non-empty, non-blank line** with **no retained CR** (`\r`)
anywhere — since `--secret-string file://...` is byte-for-byte, a stray Windows line ending would otherwise be
baked directly into the secret value.

The script also refuses if a secret with that name already exists (`expect_missing`, no silent overwrite) and
saves only the returned **ARN** to `.state/` — never the key value.

**Review checklist for this gate:** confirm `.local/providers.env` (or the `OPENAI_KEY_FILE` override path) is
never referenced by any other script, confirm the secret name matches `OPENAI_SECRET_NAME` in `env.sh`
(`medsafety/openai-api-key`), confirm `tests/unit/test_api_scripts.py`'s Gate C leak-proof tests are passing
(they assert a canary key value never appears in the script's stdout, stderr, or the AWS call log).

## Gate D — grant least-privilege read access

Rendered from `infrastructure/aws/api/policies/lambda-secrets-openai.json.tpl`:
```json
{"Version":"2012-10-17","Statement":[
 {"Sid":"ReadOpenAiApiKeySecretOnly","Effect":"Allow","Action":"secretsmanager:GetSecretValue","Resource":"<exact secret ARN from Gate C>"}]}
```
Applied via:
```
aws iam put-role-policy --role-name MedSafetyApiLambdaRole \
  --policy-name OpenAiSecretRead --policy-document file://<rendered-policy>.json
```
Run via `APPROVE_OPENAI_SECRET_POLICY=yes ./27_openai_secret_policy.sh`. The statement's `Resource` is the
literal secret ARN from Gate C — not a wildcard, not the secret name pattern, not `*`. The role gains no other
new permission (no `PutSecretValue`, `DeleteSecret`, `ListSecrets`, or KMS action — the secret uses the default
`aws/secretsmanager` key, so no separate KMS grant is needed).

**Fails closed, not overwrite:** before rendering or printing anything, the script runs
`aws iam get-role-policy --role-name MedSafetyApiLambdaRole --policy-name OpenAiSecretRead` and dies if that
succeeds — `put-role-policy` is an overwrite-if-exists AWS API, so without this check a second run (or an
unrelated name collision) would silently replace an existing statement instead of stopping for review.

**Review checklist for this gate:** confirm `Resource` is one exact ARN, confirm the policy name
(`OpenAiSecretRead`) doesn't collide with an existing inline policy on the role, confirm no other statement is
being added in the same call.

## Build (no AWS call)

```
python scripts/build_lambda.py --out .state/medsafety-api.zip --platform arm64 --provider openai
```
Bundles `fastapi`, `pydantic`, `mangum`, `httpx`, pinned `boto3`, and `openai==3.18.0` — an **exact pin, not a
floor**. `3.18.0` is the SDK version installed in `backend/.venv` when the GPT-5.6 Luna live evaluation
(`docs/PROVIDER_EVALUATION.md`: 5/5 schema success, 5/5 guard acceptance) passed; bumping `OPENAI_SDK_PIN` in
`scripts/build_lambda.py` means re-running that evaluation against the new version first, not a routine
`pip`-style upgrade. `anthropic` and `google-genai` remain forbidden in the package regardless of provider
(`verify_zip()`, unit-tested). Frozen data files re-verified against `SHA256SUMS.txt` before packaging.
Local-only; run via `30_build.sh`.

## Update the Lambda configuration

```
aws lambda update-function-code --function-name medsafety-api --zip-file fileb://.state/medsafety-api.zip --architectures arm64
aws lambda wait function-updated-v2 --function-name medsafety-api
aws lambda update-function-configuration --function-name medsafety-api \
  --role <MedSafetyApiLambdaRole ARN> --handler app.lambda_handler.handler \
  --runtime python3.12 --timeout 30 --memory-size 512 \
  --environment '{"Variables":{
    "DATA_BACKEND":"healthlake","HEALTHLAKE_DATASTORE_ID":"<current datastore id>","HEALTHLAKE_REGION":"us-east-1",
    "HEALTHLAKE_WRITE_OUTPUTS":"false","APP_STATE_BACKEND":"dynamodb","APP_STATE_TABLE":"medsafety-app-state",
    "EXPLANATION_MODE":"claude","EXPLANATION_PROVIDER":"openai","EXPLANATION_MODEL":"gpt-5.6-luna",
    "OPENAI_API_KEY_SECRET_ARN":"<exact secret ARN from Gate C>","OPENAI_TIMEOUT_SECONDS":"20","OPENAI_MAX_TOKENS":"4000",
    "DATA_PACKAGE_DIR":"/var/task/data/phase0_v1_0",
    "CORS_ORIGINS":"http://localhost:5173,http://127.0.0.1:5173","CAPTURE_REJECTED_EXPLANATIONS":"true","LOG_LEVEL":"INFO"
  }}'
```
Run via `40_lambda.sh` (already updated to emit exactly this environment set — see
`infrastructure/aws/api/40_lambda.sh`). Note what is **absent**: no `OPENAI_API_KEY`, no `BEDROCK_*` variable.
The function's `--description` is updated to `"MedSafety Phase 4 API (read-only HealthLake, OpenAI GPT-5.6
Luna -- docs/adr/0011)"` so `aws lambda get-function-configuration` self-documents the active provider.

**Automatic pre-mutation validation (no `aws_mut` call happens unless every one of these passes):**
before either `create-function`/`update-function-configuration` is invoked, the script builds the environment
dict and validates it in the same step:
- the key set is **exactly** the 16 required variables — no fewer, no more. This matters because
  `update-function-configuration` **replaces the entire `Environment.Variables` map** on every call (there is
  no merge); a missing key here would silently delete that variable from the already-deployed function, and an
  unexpected extra key is equally a bug this build should never produce.
- `OPENAI_API_KEY_SECRET_ARN` is present and non-empty.
- `OPENAI_API_KEY` is **absent** — the literal key must never be a Lambda env var.
- `HEALTHLAKE_WRITE_OUTPUTS` is exactly `"false"`.
- `HEALTHLAKE_DATASTORE_ID` is non-empty and does not equal the deleted datastore id (belt-and-suspenders with
  the early `require_current_healthlake_datastore` refusal above — both must independently pass).

Any failure exits before any AWS call is made, printing exactly which check failed.

**Review checklist for this gate:** re-run with `DRY_RUN=1` first and read the printed environment JSON; confirm
`HEALTHLAKE_WRITE_OUTPUTS` reads `"false"` after a real update too (the script asserts this by reading the
value back from AWS, with `die` on mismatch).

**Why `EXPLANATION_MODE=claude` is correct here, not a leftover:** `claude` is a legacy mode name that predates
multi-provider support, but it is provider-agnostic in behavior — see `create_explanation_service()`'s docstring
in `backend/app/services/explanation/factory.py`. It means "always attempt the configured
`EXPLANATION_PROVIDER`, skip the `auto`-mode credential-presence gate, fall back to the deterministic mock on
any failure." The already-deployed Bedrock configuration uses the same `EXPLANATION_MODE=claude` (see
`docs/PHASE4_API.md`), and `tests/unit/test_openai_explanation.py::test_explanation_mode_claude_is_provider_agnostic_legacy_naming`
proves the identical behavior for `provider=openai`: the OpenAI-specific Secrets Manager fetch still runs before
construction, and the resulting service is the real `OpenAIExplanationService`, not a name-based special case.
`EXPLANATION_MODE=auto` would also work (the secret is fetched into `OPENAI_API_KEY` before either mode's gate
runs), but `claude` is kept here for consistency with the already-deployed Bedrock configuration.

## Verification (no state mutation)

1. `aws lambda get-function-configuration --function-name medsafety-api --query 'Environment.Variables'` —
   confirm no `OPENAI_API_KEY` literal appears, only `OPENAI_API_KEY_SECRET_ARN`.
2. `python scripts/api_verify.py --mode lambda --checks A0,A2,A3,A4,A5,A6,A9,A10,A14,A15 …` — golden parity,
   read-only against HealthLake (once recreated) or the deterministic-only routes if not.
3. One real explanation call (equivalent to prior A7/A8): confirm `mode: llm` in a successful response, or a
   correctly labelled `EXPLANATION_UNAVAILABLE` fallback on any provider-side failure — never a 5xx, never an
   unlabelled response.
4. CloudWatch Logs: confirm no OpenAI key fragment, no secret ARN's `SecretString`, appears in any log line —
   same log-hygiene check as A13 in Phase 4.

## Rollback

Reverting to Bedrock (or back to the deterministic-only mock) requires no new secret/IAM/package work beyond
what already exists: rebuild with `--provider bedrock` (default) or leave `EXPLANATION_PROVIDER` unset, and
`update-function-configuration` with the Phase-4 environment set. The `OpenAiSecretRead` inline policy and the
Secrets Manager secret can be left in place (they grant no capability beyond reading that one secret) or removed
with `aws iam delete-role-policy` / `aws secretsmanager delete-secret` if no longer needed — neither is required
for rollback to succeed, since the Lambda simply stops reading `OPENAI_API_KEY_SECRET_ARN` once it's unset.

## What this plan explicitly does not do

No HealthLake recreation, no API Gateway change, no new IAM role, no wildcard IAM resource, no automatic
provider failover, no change to `/v1/ready`'s gating logic (still `clinicalStore` + `appState` only), no
change to the grounding guard, `RESPONSE_SCHEMA`, or `SafeExplanationService`.
