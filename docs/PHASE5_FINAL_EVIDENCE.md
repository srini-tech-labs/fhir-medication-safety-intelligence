# Phase 5 final evidence summary (2026-09-23)

Concise, non-secret record of the live verification results before any HealthLake lifecycle decision. No ARN
suffix, key value, or credential appears below beyond resource identifiers already treated as non-secret
throughout this repo (datastore/job ids, role/table names).

> **Superseded later the same day, 2026-09-23:** the "Resource counts" line below (`DetectedIssue 0,
> RiskAssessment 0 ... never changed`) described the state *before* the explicit FHIR write-back feature was
> deployed. That feature is now implemented and live — see `docs/CHANGE_LOG.md`'s "Explicit FHIR write-back
> deployed live" entry for the full deployment/acceptance evidence, and `docs/ARCHITECTURE.md` for the current
> architecture (analysis stays read-only; only `POST .../persist` ever writes, and only via
> `healthlake:UpdateResource`). The rest of this document is left as-is: an accurate historical snapshot of the
> Phase 5 OpenAI/HealthLake-recreation deployment at the point it was taken, not a claim about current state.

## HealthLake recreation

- Datastore: `00000000000000000000000000000001` (new id; never the deleted `00000000000000000000000000000002`), `ACTIVE`, R4, AWS_AUTH, same CMK, analytics PAUSED, NLP DISABLED, tags match the pre-deletion record.
- Import job: `00000000000000000000000000000003` — **6/6 files, 53/53/0/0** (scanned/imported/customerError/serverError).
- `healthlake_verify.py` (V0–V19): **20/20 passed**, 281 requests.
- Deterministic live parity (`tests/live_aws`, real HealthLake, mock explainer): **13/13 passed** — patient list, all 10 P001–P010 snapshot+analysis exactly match local truth, golden outcomes (P001 HIGH; P006 DDI-003+DG-001; P008 DL-001+DDI-003; P009 none; P010 NEEDS_DATA), zero derived resources written.
- Resource counts: Patient 10, Encounter 10, MedicationRequest 14, Observation 11, DocumentReference 4, Binary 4, **DetectedIssue 0, RiskAssessment 0** — reconfirmed after every subsequent live exercise (Lambda deploy, explanation calls, frontend acceptance), never changed.

## Lambda deployment (OpenAI, `gpt-5.6-luna`)

- Build: `infrastructure/aws/api/.state/medsafety-api.zip`, SHA-256 `6865c7066866af8e5c0157946a88dc2f2b77ceb7fd74cf11e4d13d78015b5703` — unchanged from freeze, `verify_zip()` passed, `openai==3.18.0` confirmed packaged, no Anthropic/`google-genai` SDK.
- Deployment: `LastUpdateStatus: Successful`, `arm64`, 512 MB / 30 s, code hash matches the frozen ZIP byte-for-byte.
- Environment: `EXPLANATION_PROVIDER=openai`, `EXPLANATION_MODEL=gpt-5.6-luna`, `EXPLANATION_MODE=claude`, `OPENAI_API_KEY_SECRET_ARN` set (an ARN pointer, not a secret), `HEALTHLAKE_DATASTORE_ID` = the new datastore, `HEALTHLAKE_WRITE_OUTPUTS=false` — no raw `OPENAI_API_KEY` present, confirmed by direct read-back from AWS.
- IAM: `OpenAiSecretRead`, `BaseAccess` (re-pointed at the new datastore ARN, plus the newly-added `healthlake:GetCapabilities`), `BedrockInvoke` all confirmed correctly scoped, least-privilege, no wildcards.

## Real cloud OpenAI invocation

- One real explanation call for P001, both via direct Lambda invoke and through the live HTTPS API Gateway path: `mode: llm`, `model: gpt-5.6-luna`, `groundedInFindingsOnly: true`, restating exactly the deterministic finding (`DL-001`, HIGH) with no new claims, `fallbackCode: null`.
- Deterministic finding stayed authoritative throughout; the grounding guard was exercised on real model output (not a fake), and passed.

## API Gateway / readiness

- `/v1/health` → `200 {"status":"ok"}`.
- `/v1/ready` → `200 {"status":"ready","checks":{"clinicalStore":"ok","appState":"ok","explanationProvider":"configured"}}` while HealthLake is `ACTIVE` — no ARN, hostname, secret, or stack trace in the body.
- Source-IP allowlist unchanged (confirmed via `get-rest-api --query policy` before and after adding the route).
- Final cloud acceptance suite through the live API Gateway: **12/12 passed** (health, undocumented-routes-blocked, patient list, snapshots, documents, golden analyze parity, persistence, unknown-id 404, CORS, throttling 429-not-5xx, HealthLake-unchanged, write-safety).

## Frontend acceptance

- 17/17 frontend unit tests pass; production build succeeds and correctly embeds the live API Gateway URL.
- Exercised the four representative demo cases against the live API with the frontend's exact origin (`http://localhost:5173`) and CORS headers confirmed present and correct: **P001 HIGH, P006 interaction (DDI-003) + data gap (DG-001), P009 negative (no findings), P010 NEEDS_DATA** — all match golden expectations exactly.
- AI explanation displayed correctly through the same live path (see above).

## Final quality gate

- Backend: **870 passed, 1 skipped** (pre-existing opt-in real-network build test), 13 deselected.
- Frontend: **17/17 passed**, build succeeds.
- Secret scan: no real credential-shaped strings in any tracked file (all matches traced to labelled test fixtures); `.state/`, `.local/`, `.aws-local/`, `.env*` confirmed git-ignored and untracked.
- git-status audit: 14 legitimate files changed and committed (code, tests, docs); no `.state`, ZIP, credential, or generated-report file staged.

## Two real bugs found and fixed by deploying live (neither caught by any prior offline test)

1. `50_import.sh`'s manifest-count parser crashed on HealthLake's real key ordering (matched `successOutput`, a nested object, instead of the resource-count field) — fixed, regression-tested against the real script's own logic.
2. `/v1/ready`'s clinical-store probe 403'd: the deployed Lambda role was missing `healthlake:GetCapabilities` (distinct from `ReadResource`/`SearchWithGet`) — fixed, and a structural test now ties the deployed IAM template to the offline test fake so this class of drift can't recur silently.

## Datastore status at the end of this session

**`00000000000000000000000000000001` is `ACTIVE`.** Deletion was explicitly withheld per instruction and requires
separate, explicit approval before it proceeds. Billing continues (~$0.27/hour) until that approval is given and
the deletion is carried out.
