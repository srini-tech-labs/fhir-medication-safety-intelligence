# Phase 4 — React → API Gateway → Lambda → HealthLake → engine → Bedrock

> **Update 2026-09-19:** the HealthLake datastore was deleted to stop the hourly charge; the deployed API's data routes now return 503 until it is recreated (see `docs/CHANGE_LOG.md`). The results below were obtained while it existed.

**Status: Cloud application path complete; Bedrock AI explanation pending AWS account quota enablement.**
The deterministic path React → API Gateway → Lambda → HealthLake → engine → DynamoDB is deployed and verified end to end. The Bedrock
explanation provider (Converse, **Amazon Nova 2 Lite** `us.amazon.nova-2-lite-v1:0`, forced tool, reasoning disabled; DynamoDB with default
AWS-owned encryption) is implemented, tested and deployed, but **Nova cannot be invoked in this account yet**, so acceptance checks **A7/A8
are PENDING** (never run, not counted as passed). The explanation endpoint returns the existing labelled `EXPLANATION_UNAVAILABLE` fallback.
The Anthropic / Sonnet 5 path is **paused** (no account upgrade, plan change, Marketplace agreement or first-time-use form was made).
Read-only against HealthLake: `HEALTHLAKE_WRITE_OUTPUTS=false` and the Lambda role has no HealthLake write action.

```
React (local Vite, VITE_API_BASE_URL)
  → API Gateway REST (source-IP resource policy, throttling, exactly the 7 contract routes)
    → Lambda `medsafety-api` (FastAPI via Mangum; python3.12 arm64)
        → HealthLakeFHIRRepository (SigV4, read + search only) → HealthLake (Phase 3 datastore, unchanged)
        → deterministic rule engine → Analysis (saved in DynamoDB `medsafety-app-state`)
        → SafeExplanationService → BedrockClaudeExplanationService (Converse, Nova forced tool) → schema re-validation → guard → AIExplanation
```

## What was built (all offline-tested)
| Piece | File | Notes |
|---|---|---|
| Lambda entry | `backend/app/lambda_handler.py` | `Mangum(app, lifespan="off")`; INFO root level, `mangum` logger pinned WARNING |
| Store-failure handling | `backend/app/main.py`, `repository/base.py` | `StoreUnavailable` → generic 503 `{"detail":"Clinical data store temporarily unavailable"}`; one JSON access line per request (id, route template, status, ms) |
| App state | `repository/app_state_dynamodb.py` | atomic counter, strongly consistent reads, JSON-string documents, TTL 30 d captures; only Get/Put/Update/Query |
| Bedrock provider | `services/explanation/bedrock.py` | one `converse` call. **Nova (`tool` mode):** `record_explanation` is the only tool, `toolChoice.tool` forces it, `reasoningConfig: disabled`, `maxTokens 4000`, `temperature 0.00001`; the answer is `toolUse.input`. The Nova *generation* schema is derived from `RESPONSE_SCHEMA` (no `additionalProperties`, no `anyOf`; only `type/properties/required`), and the output is **re-validated against the original `RESPONSE_SCHEMA`** (`schema_check.py`, fail-closed) before the unchanged guard. Claude `text_format` mode kept, paused. 1 attempt, 3 s connect / 20 s read; a rejected schema/parameter is reported, not worked around |
| Contract validator | `services/explanation/schema_check.py` | tiny fail-closed JSON-Schema validator; agrees with `jsonschema` on a 4,000+ document corpus |
| Shared explanation code | `services/explanation/claude.py` | mechanical extraction of `_complete()` / `_finish()`; Anthropic path and guard unchanged |
| Package | `scripts/build_lambda.py` | arm64 zip ≈ 20 MB; 7 frozen files verified against `SHA256SUMS.txt`; boto3/botocore pinned 1.43.98; no Anthropic SDK |
| Deployment | `infrastructure/aws/api/` | scripts, policy templates, `routes.txt` (= the app's OpenAPI routes, asserted by a test) |
| Verification | `scripts/api_verify.py` | A0–A16; direct-Lambda, HTTPS and in-process transports |

## Offline validation
- 660 backend tests (+ 1 opt-in) and 17 frontend tests pass; all ten golden scenarios and snapshots through the **Lambda handler** equal the local backend.
- Requests to Bedrock are validated against the real botocore Converse service model (Stubber). Mutation-checked: role gains a write/Scan/KMS action, `bedrock:*`, SSE spec on the table, write-outputs true, IP range check removed, `effort` changed, retry count.
- Real-dependency build (opt-in): the unzipped host-arch package serves `/v1/health` from a clean interpreter with the Anthropic SDK blocked.
- Two real defects were caught by these tests and fixed: botocore timeouts have `response=None`; botocore `max_attempts=1` means **two** tries (now `total_max_attempts=1`).

## Live results (2026-09-19)
**Deployed:** DynamoDB `medsafety-app-state` (on-demand, default AWS-owned encryption), log group `/aws/lambda/medsafety-api` (30 days), role `MedSafetyApiLambdaRole` (HealthLake read/search, DynamoDB get/put/update/query, logs, and the three approved Gate B Nova statements), Lambda `medsafety-api` (python3.12, arm64, **512 MB** — this young account's Lambda memory limit; the plan said 1024 — timeout 30 s, no VPC, package 19.7 MB, `HEALTHLAKE_WRITE_OUTPUTS=false`), API Gateway REST API `medsafety-api` (`a1b2c3d4e5`, stage `dev`): `https://a1b2c3d4e5.execute-api.us-east-1.amazonaws.com/dev`.
API Gateway read-back: exactly the 7 contract routes + `OPTIONS`, `AWS_PROXY` to the one Lambda, 29 s integration timeout, stage throttling 5 rps / burst 10 (explanation route 2 / 3), regional, source-IP resource policy (single /32), tags, no execution logging; `/docs`, `/openapi.json`, `/redoc`, `/` → 403.

**Direct-invoke suite** (`--mode lambda`, live HealthLake, before API Gateway existed): 12/12 pass; ten analyses = golden = local backend; HealthLake unchanged.

**HTTPS suite** (`60_verify.sh`, allow-listed IP, final run: 59 requests):
| Check | Result |
|---|---|
| A0 health · A1 undocumented routes blocked · A2 patient list (10) · A3 snapshots = local · A4 documents = frozen notes | PASS |
| A5 analyze all ten → golden + local parity (P006 DDI-003 + DG-001, P008 DL-001 then DDI-003, P009 none, P010 NEEDS_DATA) | PASS |
| A6 persisted in DynamoDB, numbering, default encryption | PASS |
| **A7 explain all ten × runs · A8 idempotent explain** | **PENDING** — Bedrock AI explanation pending AWS account quota enablement; no model call made; acceptance criteria unchanged (floor 80 %) |
| A9 unknown ids → 404 · A10 CORS preflight · A12 latency (max 536 ms, all < 29 s) | PASS |
| A11 throttling answers 429, never 5xx | PASS in the final run (sustained 160 requests: 120×200, 40×429, 0×5xx; concurrent burst of 40: 19×200, 21×429, 0×5xx) — **but see finding below** |
| A13 log hygiene (1,277 log events, 313 access lines; only the five approved fields; no names/notes/model output/credentials) | PASS |
| A14 HealthLake unchanged (10/10/14/11/4/4, all `versionId 1`, 0 DetectedIssue/RiskAssessment) | PASS |
| A15 write-safety (role = read + search only, Bedrock actions ⊆ {InvokeModel, GetInferenceProfile}, no Marketplace, no secrets in env, `HEALTHLAKE_WRITE_OUTPUTS=false`) | PASS |
| **A16 browser smoke** (local Vite → API Gateway, real Chromium; P001/P006/P008/P010 rendered correctly, "API connected", zero console errors, only the API host contacted, labelled fallback shown, no provider detail leaked) | PASS |
| F1 labelled fallback served correctly from a stored analysis | PASS in run 1 (evidence kept); SKIP in the final run — the check is read-only and makes no model call, so it SKIPs when the latest analysis has no stored explanation |

**Fallback behaviour (verified without any additional model call):** the two real Gate B attempts through the deployed Lambda role produced the labelled fallback exactly as designed; F1 read that stored fallback back over HTTPS (`mode: mock`, code `EXPLANATION_UNAVAILABLE`, public reason only, deterministic findings unchanged, no provider text, no model output); offline tests cover every failure category; the browser showed "The AI explanation service is unavailable. A deterministic summary is shown instead." next to the unchanged deterministic layers. The browser run answered the UI's automatic explanation requests inside the browser (route interception with the real fallback body), so **no explanation request reached API Gateway or Bedrock**; the Lambda log has zero explanation access lines for that window.

**Finding — A11 concurrency (intermittent, not fully explained).** In one earlier run a burst of 40 simultaneous requests produced HTTP 500 `InternalServerErrorException` from API Gateway (no Lambda error was logged); the final run's burst produced only 200/429. Sustained throttling (429, never 5xx) was reproduced separately (160 requests at ≈ 24 req/s: first 429 at request #31, zero 5xx). The most likely cause is this young account's Lambda concurrency limit under a 25+ simultaneous burst (unconfirmed: the deployer cannot read account Lambda settings or CloudWatch metrics). Impact for a single-user, IP-allow-listed prototype: low; it is reported, not hidden. The check was made stricter (sustained **and** concurrent), not looser.

**Bedrock / Nova status.** Gate B statements applied exactly as approved. Call 1 (16:25Z): `AccessDeniedException` "Your account is currently being verified…". Retry (~18:27Z, as instructed): `ValidationException: Operation not allowed`. Both returned no tokens; the response was the labelled fallback. An AWS Support case is open. No further Nova call was made after that, and none was made during this verification.
**Resume plan** (only when AWS enables Nova): re-run the one-model-call test at Gate B, then A7/A8 (`--checks …,A7,A8` without `--pending`), with no change to the deployment, IAM, model, schema or acceptance floor.

Defects found in my own tooling and fixed with regression tests: A13 log-prefix parsing/eager f-string/scan-before-delivery; A9/A13 accounting of API-Gateway-only answers (429/403 never reach Lambda) and paced calls; the stage-throttling patch (braces in `{patient_id}`) now uses a JSON patch file; `60_verify.sh` now passes the Phase 3 verifier role so A14 is never silently skipped.

## Known risks (verified live, reported not assumed)
Nova invocation is **still unproven** (rejected twice at Gate B: account verification, then `Operation not allowed`; AWS Support case open) · whether `bedrock:InvokeModel` alone suffices for Converse · documented Nova parameter conflicts (`maxTokens` 5,000 vs examples 10,000; `temperature` minimum 0.00001 vs examples 0) · possible first-use latency of constrained decoding · no Nova guard baseline (floor 80 %, below stops the phase) · `aws:ResourceTag` support for API Gateway actions · source-IP allowlist if your IP changes.

## Cost (list prices; re-verified at deployment)
HealthLake datastore (already running) ≈ $0.27/h dominates. Bedrock Nova 2 Lite via the US geo profile: list price not verifiable from the pages I could read; even at an assumed ≈ $0.5/$3 per MTok ≈ $0.002 per explanation (30 + smoke < $0.1). Lambda, API Gateway, DynamoDB, CloudWatch each < $0.05. Incremental ≈ $0.5–1; idle ≈ $0.
