# Phase 4 deployment — API Gateway → Lambda → HealthLake → OpenAI (us-east-1)

**Deployed provider as of docs/adr/0011: OpenAI (`gpt-5.6-luna`), live and verified 2026-09-23** (see
`docs/PHASE5_OPENAI_DEPLOYMENT.md` for the executed command record and results). Bedrock/Nova (Revision 3, Amazon
Nova 2 Lite; see `docs/PHASE4_API.md`) remains implemented, unchanged, and selectable via
`EXPLANATION_PROVIDER=bedrock` (`25_bedrock_policy.sh` / `--provider bedrock` on `build_lambda.py`) but is not the
deployed target -- its explanation path is pending AWS account quota enablement (open Support case). Scripts are
idempotent, `set -euo pipefail`, and support `DRY_RUN=1` (mutating calls are printed, not run). No credentials in
any file: `source .env.aws` (profile `medsafety`) first. **Nothing here modifies HealthLake, KMS, or the buckets;
IAM role/policy changes are limited to the additive, scoped statements Gate D and the `FhirReadinessProbe`
statement add.** HealthLake is read-only from Phase 4.

## Gates (the scripts stop; nothing is retried, widened or switched automatically)
| Gate | Where | What you approve / what stops the run |
|---|---|---|
| A | before anything | you attach `MedSafetyPhase4DeployerPolicy` (rendered by `make aws-api-deployer-policy ACCT=<id>`, additive; existing policies untouched) |
| Preflight | `00_preflight.sh` | (Bedrock path only) **exit 3** = account enablement needed. **exit 4** = inference profile not as planned. Not required for the OpenAI target. |
| B | `25_bedrock_policy.sh` | (Bedrock path only, not the current target) the exact `bedrock:InvokeModel` statements; applied only with `APPROVE_BEDROCK_POLICY=yes` |
| C | `26_openai_secret.sh` | creates the Secrets Manager secret holding the production OpenAI API key, read by default from `OPENAI_API_KEY` in the git-ignored `.local/providers.env` (only that one line; nothing else in the file is read), or from `OPENAI_KEY_FILE` if set (optional override, validated: outside the repo, mode 600/400, exactly one CR-free key line); the value is written straight into a fresh mode-600 temp file by a single python3 process (never a shell variable, never printed) and removed by an `EXIT` trap on every exit path; applied only with `APPROVE_CREATE_OPENAI_SECRET=yes` |
| D | `27_openai_secret_policy.sh` | the exact `secretsmanager:GetSecretValue` statement (scoped to that ONE secret ARN) printed for review; applied only with `APPROVE_OPENAI_SECRET_POLICY=yes`; **fails closed** if an `OpenAiSecretRead` inline policy already exists on the role, rather than overwriting it |
| HealthLake freshness | `40_lambda.sh` | refuses to run at all (before any AWS call) unless `HEALTHLAKE_DATASTORE_ID` is non-empty and differs from the datastore id recorded in `infrastructure/aws/healthlake/datastore-recreation-record.json` — i.e. HealthLake must actually be recreated first, not just have a non-empty (possibly stale) id |
| Schema | first real chat-completion call | a 400 about the JSON schema/response_format, an auth failure, or a rate limit → report; no silent fallback |
| IP | `50_api.sh` | `ALLOWED_IP_CIDR=<your public IPv4>/32` — open ranges are refused |

## Run order (OpenAI target)
| Step | Script | Creates |
|---|---|---|
| 1 | `10_state.sh` | DynamoDB `medsafety-app-state` (on-demand, **default AWS-owned encryption**, TTL `ttl`) + log group (30 days) |
| 2 | `20_role.sh` | `MedSafetyApiLambdaRole` — HealthLake read/search, DynamoDB get/put/update/query, logs |
| 3 | `APPROVE_CREATE_OPENAI_SECRET=yes 26_openai_secret.sh` (reads `.local/providers.env`; or add `OPENAI_KEY_FILE=…` to override) | **Gate C**; Secrets Manager secret holding the OpenAI key — no HealthLake dependency, runnable while HealthLake is deleted |
| 4 | `APPROVE_OPENAI_SECRET_POLICY=yes 27_openai_secret_policy.sh` | **Gate D**; scoped `secretsmanager:GetSecretValue` on the role — no HealthLake dependency, runnable while HealthLake is deleted |
| 5 | `30_build.sh` | arm64 zip, `--provider openai` (local; frozen data checksum-verified; pinned boto3 + `openai==3.18.0`, exact pin — see `docs/PHASE5_OPENAI_DEPLOYMENT.md`; no Anthropic/Gemini SDK) — no HealthLake dependency, runnable while HealthLake is deleted |
| 6 | `40_lambda.sh` | Lambda `medsafety-api` (python3.12, arm64, 512 MB, 30 s, `HEALTHLAKE_WRITE_OUTPUTS=false`, `EXPLANATION_PROVIDER=openai`, `OPENAI_API_KEY_SECRET_ARN` set, no `OPENAI_API_KEY` env var) — **requires HealthLake to be recreated first** (see the "HealthLake freshness" gate above); automatically validates the complete environment-variable set before any mutating call |
| 7 | `python scripts/api_verify.py --mode lambda --checks A0,A2,A3,A4,A5,A6,A9,A10,A14,A15 …` | direct-invoke golden parity against live HealthLake (no API Gateway yet) |
| 8 | `ALLOWED_IP_CIDR=… 50_api.sh` | REST API `medsafety-api`, exactly the 8 contract routes (incl. `/v1/ready`) (+OPTIONS), IP resource policy, throttling, stage `dev` — re-running this script only adds a genuinely new route/resource; an existing API's resource policy is not re-applied, so the allowlisted IP is preserved automatically |
| 9 | `60_verify.sh` | A0–A16 over HTTPS |
| — | `70_update_allowlist.sh` | after your IP changes |
| — | `CONFIRM_TEARDOWN=yes 90_teardown.sh` | removes Phase 4 only |

For the Bedrock target instead, insert `00_preflight.sh` first and `APPROVE_BEDROCK_POLICY=yes 25_bedrock_policy.sh`
in place of steps 3–4, and build/deploy with `EXPLANATION_PROVIDER=bedrock` (the default `build_lambda.py --provider`).

State (ids, ARNs, rendered policies, the zip) lives in `.state/` (git-ignored, no secrets -- the secret VALUE is
never written to any state file; only its ARN is, via `save_state OPENAI_SECRET_ARN`).
