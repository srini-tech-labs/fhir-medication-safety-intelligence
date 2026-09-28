# Phase 3 deployment — AWS HealthLake (us-east-1)

Everything here is idempotent, `set -euo pipefail`, and supports `DRY_RUN=1` (mutating calls are printed, not run). No script
contains credentials: you configure a named profile yourself and export `AWS_PROFILE`.

## Before running anything
1. Configure a profile for a **non-root** identity (`aws configure sso --profile medsafety` or an IAM user). Never paste keys anywhere.
2. Attach the deployer policy to that identity (render it with your account id; review it first):
   `make aws-deployer-policy ACCT=<12-digit-id> > deployer-policy.json` — name it `MedSafetyPhase3DeployerPolicy`.
   It is scoped by name/tag (no `AdministratorAccess`). Datastore-scoped HealthLake actions use
   `arn:aws:healthlake:us-east-1:<acct>:datastore/fhir/*`, with `aws:ResourceTag/Project=medsafety-intelligence` on every datastore-scoped action (describe, delete, tagging, import-job start/describe/list).
   `CreateFHIRDatastore` (and `TagResource`, for tag-on-create) stays `Resource:"*"` gated by `aws:RequestTag/Project`; list stays `*`.
   One policy, attached once; there is no second stage.
3. `export AWS_PROFILE=medsafety` (region is pinned to us-east-1 in `env.sh`).

## Run order (the datastore bills $0.27/hour from step 3 until step 9)
| Step | Script | Creates |
|---|---|---|
| 1 | `00_preflight.sh` | nothing (read-only): identity, principal, existing datastores, name availability, package checksums |
| 2 | `10_foundation.sh` | KMS CMK (+alias, rotation), import and results buckets (hardened) |
| 3 | `20_datastore.sh` | HealthLake datastore (R4, `AWS_AUTH`, CMK) → waits for ACTIVE |
| 4 | `30_iam.sh` | import role (trust = datastore ARN), App role, Verifier role |
| 5 | `40_stage_upload.sh` | the six frozen NDJSON files, byte-for-byte, SHA-256 verified |
| 6 | `50_import.sh` | strict bulk import; stops unless COMPLETED with 53 scanned / 53 imported / 0 errors |
| 7 | `60_verify.sh` | V0–V19 as the Verifier role + live golden parity as the App role |
| 8 | *(decision)* | keep or tear down — never automatic |
| 9 | `CONFIRM_TEARDOWN=yes 90_teardown.sh` | deletes datastore, roles, buckets (all versions); schedules key deletion (7-day minimum) |

State (ids, ARNs, rendered policies — no secrets) is kept in `.state/` (git-ignored).

## Datastore deleted without a full teardown, and how to recreate it

The datastore (only) was deleted on 2026-09-19 to stop its hourly charge, without running `90_teardown.sh` — the
buckets, KMS key, and IAM roles were kept because Phase 4 and the import artifacts still depend on them. Its full
configuration and everything needed to recreate it identically is recorded in
[`datastore-recreation-record.json`](datastore-recreation-record.json) (committed before the deletion).

`91_recreate.sh` is a **safe, non-mutating preflight** built from that record — see its header comment for the
full list of checks. It creates nothing: it verifies the recorded dependencies (CMK, IAM roles, S3 object counts)
still match live state, refuses to reuse either AWS client token recorded for the deleted resources, clears only
the datastore-specific stale identifiers from `.state/deploy.env` (backed up first), and prints the exact by-hand
command sequence — steps 3, 4, 6 above plus a shortened parity check
(`scripts/healthlake_verify.py --checks V0,V1,V2,V3,V4,V5`) before the full V0–V19, then the matching Phase 4
role/Lambda/env updates and a shortened API-level check. It has been implemented and tested against a stub `aws`
(`tests/unit/test_healthlake_recreate.py`) but **has not been run against the real account** — recreation needs
explicit approval before either the preflight or the by-hand steps are executed.

```
RECREATE_DS_CLIENT_TOKEN=<new, never used>  RECREATE_JOB_CLIENT_TOKEN=<new, never used>  CONFIRM_RECREATE=yes ./91_recreate.sh
```

## Identity handling
`resolve_principal` trusts the **long-term IAM role or user** in the App/Verifier trust policies. If your session is
`arn:aws:sts::<acct>:assumed-role/<role>/<session>` it resolves the underlying role with `iam:GetRole` (SSO roles keep their
`aws-reserved/sso.amazonaws.com/` path); override with `PRINCIPAL_ARN=...`. Root and session ARNs are refused.

## Stop conditions (the scripts stop; nothing is retried, widened or relaxed automatically)
- import trust `AssumeRole` denied → report (no second ARN variant is tried);
- any import rejection or id change → report the resource, error and cause; validation stays `strict`; frozen files are never edited;
- an AccessDenied that needs a policy addition → report the exact action; add nothing without approval.
