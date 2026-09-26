# Phase 3 — AWS HealthLake core

**Status:** complete. The frozen dataset was imported under strict validation (53/53), V0–V19 passed (20/20), and live golden parity passed (13/13). The original Phase 3 datastore was later recreated as part of the subsequent deployment phases; the current demo datastore remains intentionally **ACTIVE** for portfolio/demo use. See the Phase 5 documentation for the current deployed state.

## What Phase 3 delivers

AWS HealthLake (us-east-1, FHIR R4, SigV4 `AWS_AUTH`, customer-managed KMS key) becomes the cloud FHIR system of record for the
frozen synthetic dataset, behind the existing `ClinicalRepository` interface.

```
data/phase0_v1_0/fhir/bulk/*.ndjson  (53 resources, frozen, checksummed)
        │  byte-for-byte upload, SHA-256 verified                          S3 = staging only
        ▼
S3 import bucket (SSE-KMS)  ──►  HealthLake native bulk import (--validation-level strict)
                                          │
                                          ▼
                         HealthLake datastore  ◄── FHIR REST + SigV4 ──  HealthLakeClient
                                                                              │
                                                       HealthLakeFHIRRepository (ClinicalRepository)
                                                                              │  same parse_bundle()
                                                            rule engine · snapshot · explanation (unchanged)
```

Not touched: React UI, deterministic rules, golden expectations, the P006 override, AI-explanation presentation, `LocalFHIRRepository`,
the frozen dataset. Not built (later phases): Lambda, API Gateway, Bedrock, SMART on FHIR, CSV/C-CDA agent.

## Design decisions

| Decision | Reason |
|---|---|
| Reuse `parse_bundle()`: the repository assembles the patient's HealthLake resources into a Bundle dict and parses it with the same code as the local backend | Rule inputs are identical by construction; parity is asserted for all ten patients |
| One search per patient: `Patient?identifier=<system>\|Pxxx&_revinclude=<Type>:patient` for MedicationRequest, Observation, Encounter, DocumentReference; `Binary/{id}` for notes | Few round-trips; the Encounter is required because `scenario_label` comes from `Encounter.reasonCode` |
| `_revinclude` spelling falls back `patient` → `subject` → per-type searches, **only on HTTP 400** | Live behaviour of the spelling is unverified; silent empties are prevented by the live parity test |
| App id `P001` ↔ FHIR id `patient-p001` resolved by identifier search, never by changing frozen ids | `Patient/P001` is a documented 404 |
| Writes (`DetectedIssue`, `RiskAssessment`) are PUT with deterministic ids (idempotent upsert, history kept) but **disabled by default** (`HEALTHLAKE_WRITE_OUTPUTS=false`) | Approved plan keeps P001–P010 free of derived resources in Phase 3; the content is in the saved analysis JSON. The write path is covered offline and by V11–V16 on temporary data |
| Application state (analysis JSON, rejected-output captures) stays in the local output store | Approved decision; `AppStateStore` is swappable for the Lambda phase |
| Strict validation everywhere (`--validation-level strict` on import, `x-amzn-healthlake-fhir-validation-level: strict` on writes) | Never weakened; a test fails if any deployment script or policy uses `minimal`/`structure-only`, and the client defaults to `strict` |
| SigV4 with botocore over httpx; retries with jitter on 429/5xx; paging follows only same-datastore `next` links; logs carry no bodies, ids or query values | httpx INFO and botocore DEBUG would otherwise log patient identifiers and signatures — pinned via `harden_sdk_logging` and covered by a sensitivity-probed test |
| Runtime identity = **App role** (read/search/create/update); History, vread, ProcessBundle and Delete exist only on the temporary **Verifier role** | Least privilege; offline tests assert the repository issues only `ReadResource`/`SearchWithGet` |

## Resources (all tagged `Project=medsafety-intelligence, Phase=3, Environment=dev, DataClass=synthetic`)

| Resource | Name |
|---|---|
| KMS CMK (symmetric, rotation on) | `alias/medsafety-healthlake-cmk` |
| S3 import / results buckets | `medsafety-hl-import-<acct>-us-east-1`, `medsafety-hl-results-<acct>-us-east-1` (prefix `phase0-v1/`) |
| HealthLake datastore | `medsafety-fhir-r4` |
| IAM roles | `MedSafetyHealthLakeImportRole`, `MedSafetyHealthLakeAppRole`, `MedSafetyHealthLakeVerifierRole` |
| Import job | `medsafety-phase0-v1-import` |

IAM/KMS/S3 policy templates: `infrastructure/aws/healthlake/policies/`. Datastore-scoped resources and the import-role trust use
`arn:aws:healthlake:us-east-1:<acct>:datastore/fhir/<datastoreId>` (built from account and id, not copied from the control-plane
`DatastoreArn`). Run instructions: `infrastructure/aws/healthlake/README.md`.

## Verification (V0–V19, `scripts/healthlake_verify.py`)

V0 capability statement · V1 read `patient-p001` (v1, `Patient/P001` = 404) · V2 identifier search · V3 accurate counts per type ·
V4 patient-scoped searches · V5 Binary note equals frozen note · V6 `_include` · V7 `_revinclude` · V8 `_elements`/SUBSETTED ·
V9 paging + `_sort` · V10 chaining + `_has` · V11 POST create · V12 PUT create + `If-None-Match` · V13 update, `If-Match`, history, vread ·
V14 POST idempotency key · V15 search consistency lag · V16 batch/transaction bundles · V17 strict validation rejects an invalid
resource · V18 authorization negatives (unsigned, App role delete/history/bundle denied, App role PUT allowed) · V19 cleanup and proof
that all 53 frozen resources are still `versionId 1`, history length 1, content equal to the frozen file.
All writes use temporary `zz-phase3-tmp-*` data tagged `…/phase3-test|temp`. Observations that the docs leave open (type-level
`_history`, `_summary` with `_elements`, `_revinclude` spelling) are recorded rather than assumed.

## Offline validation (done, no AWS)

| Check | Result |
|---|---|
| Backend tests (`make test-backend`) | 428 passed (13 live tests deselected) |
| Frontend typecheck + tests | passes, 17 tests |
| Golden cases through `HealthLakeFHIRRepository` (fake HealthLake) | all ten patients: domain objects, snapshots and deterministic analyses **equal to `LocalFHIRRepository`** |
| V0–V19 against the fake | all pass; negative tests show the suite detects tampering, a missing import, lenient validation, an over-permissive App role and non-atomic transactions |
| Policy tests | datastore ARN form, single `SourceArn` trust condition, App vs Verifier action sets, no wildcards, deployer scope |
| Script tests (stub `aws`) | preflight strictly read-only; root refused; assumed-role → underlying role; IAM step refuses to run before the datastore exists; exact create/import/upload flags; teardown needs confirmation |
| Frozen package | `sha256sum -c SHA256SUMS.txt` OK; 53 resources valid FHIR R4 in the local preflight |
| Local FHIR preflight (`fhir.resources==6.4.0`, throw-away venv) | PASSED; 4 watch items: relative `Attachment.url` `Binary/<id>` in `docref-p002/p006/p008/p010-note` (legal in R4, rejected by that library) — to observe under HealthLake `strict` |

## Live results (2026-09-19, us-east-1, account 123456789012)

**Datastore** `medsafety-fhir-r4` · id `00000000000000000000000000000002` · ACTIVE · FHIR R4 · authorization `AWS_AUTH` (fine-grained off) ·
customer-managed KMS key `00000000-…` · analytics `PAUSED` · NLP `DISABLED` · endpoint `https://healthlake.us-east-1.amazonaws.com/datastore/00000000…/r4/` ·
ARN `arn:aws:healthlake:us-east-1:123456789012:datastore/fhir/00000000…` (**the `datastore/fhir/<id>` form is the real ARN**) · tags Project/Phase/Environment/DataClass.
Creation took ≈ 15 minutes (05:45:38 → ACTIVE 06:01).

**Import** job `00000000000000000000000000000004`, `--validation-level strict`: COMPLETED in ≈ 1 min; 6/6 files; **53 scanned, 53 imported, 0 customer errors, 0 server errors**;
no `FAILURE/` output. The four relative `Attachment.url` (`Binary/<id>`) were accepted by strict validation. Resource ids were preserved (`patient-p001` … `patient-p010`).

**V0–V19: 20/20 pass** (final run, 277 requests; report kept in the git-ignored `.state/verification-report.json`), and **live golden parity: 13/13 pass** —
snapshots and deterministic analyses for all ten patients equal `LocalFHIRRepository`; P001 HIGH, P006 DDI-003 + DG-001, P008 DL-001 then DDI-003, P009 none, P010 NEEDS_DATA;
the analysis wrote no DetectedIssue/RiskAssessment. All 53 frozen resources: `versionId 1`, history length 1, content equal to the frozen files; final counts 10/10/14/11/4/4.

**Behaviours measured (not assumed)**
| Topic | Observed |
|---|---|
| `_revinclude` spelling | `:patient` accepted (no fallback needed) |
| Version history | **eventually consistent**: empty for minutes after import/updates (measured 72 s after an update, ≈ 5+ min after import); the `x-amz-fhir-history-consistency-level: strong` header did not change that |
| Search / totals | `_search` lag ≈ 20 s after a write; `_total=accurate` briefly still counted a just-deleted resource |
| POST create | 201, id in body + `ETag W/"1"`; **no `Location` header** |
| POST idempotency key | duplicate → **409** `OperationOutcome` (`code: duplicate`); the original is named in the **`Location` header**, not the body |
| `PUT` + `If-None-Match: *` | works; a deleted id stays taken (412) and reads return 410 — temp ids must be unique per run |
| Paging | `next` links carry raw `==` in the page token; they must be percent-encoded before SigV4 signing (client bug found and fixed) |
| `_summary` + `_elements` | rejected (400) |
| Type-level / system `_history` | denied for the Verifier role: needs `healthlake:GetHistoryByResourceType` / `healthlake:GetFullHistory` (not added) |
| Unsigned request | 401 (plan expected 403) |
| Strict validation | `Observation` without `status` → 400 with the FHIR minimum-cardinality message |

**Deviations from the approved plan (each reported when it occurred)**
1. Creation required `ram:GetResourceShareInvitations` and `ram:AcceptResourceShareInvitation` (HealthLake probes them; the accept call uses a dummy `ffffffff-…` invitation id and is expected to answer "not found"), plus `glue:CreateDatabase` and Lake Formation data-lake-administrator status for the deployer role (probe: a "Test Database created from HealthLake service"). Set up by the account owner; **the plan's "no Lake Formation" assumption did not hold**.
2. `--analytics-configuration Status=DISABLED` is rejected at creation (ENABLED|PAUSED only); the datastore was created with `PAUSED` (owner's decision). HealthLake created a Glue resource-link database `medsafety_fhir_r4_<id>_healthlake_view` in the account; the teardown script removes it by exact name.
3. Import role needed `s3:ListBucket` on the results bucket (prefix-limited to `phase0-v1/`) — added with approval.
4. Deployer policy: added `s3:GetLifecycleConfiguration` (my omission) and the RAM statements.
5. Verifier bugs found and fixed against reality (SigV4 next-link encoding, POST/409 shapes, history/total eventual consistency, per-run temp ids, `Manifest.json` capitalisation). No frozen data, rule, or validation level was changed.

## Risks and unknowns (status after the live run)

1. Bulk import preserves resource ids — **confirmed** (V1/V19).
2. The `datastore/fhir/<id>` ARN form in IAM — **confirmed** (import-role trust worked; App role read works; V18 negatives).
3. `_revinclude` spelling — **`:patient` works**; minimum import-role S3/KMS permissions — **needed `s3:ListBucket` on the results bucket** in addition to the plan.
4. Relative `Attachment.url` under strict validation — **accepted**.
5. Search and history are eventually consistent — **measured** (≈ 20 s search, minutes for history); code that reads history or totals right after a write must poll.
6. Still open: `x-amz-fhir-history-consistency-level` did not give strong history here; analytics is `PAUSED` but its Glue link database exists until teardown.

## Cost

HealthLake bills **$0.27 per datastore-hour** (≈ $6.48/day, ≈ $194/month if left running; 10 GB storage and 3,500 queries/hour included;
import free). Everything is built and tested locally first and the datastore is created as late as possible. KMS ≈ $1/month prorated
(the key waits ≥ 7 days for deletion); S3, IAM, STS, CloudTrail Event history are negligible or free.
