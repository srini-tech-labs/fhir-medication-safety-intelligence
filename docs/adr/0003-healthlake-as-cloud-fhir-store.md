# ADR-0003: AWS HealthLake as the cloud FHIR system of record, read-mostly

**Status:** implemented (Phase 3); datastore currently **deleted** to stop billing (ADR-0010) — decision unchanged.

## Context
Phase 3 needed a real managed FHIR server, not another local simulation, to prove the repository abstraction
(ADR-0002) and to give Phase 4 something real to call over the network.

## Decision
AWS HealthLake, FHIR R4, `AWS_AUTH` (SigV4, no SMART/OAuth — out of scope), customer-managed KMS key, analytics
`PAUSED` (not `DISABLED`: the API rejects `DISABLED` at creation — real finding). Import is HealthLake's native
bulk import under `--validation-level strict` from S3 (staging only, never an application backend). Runtime access
uses least privilege: an **App role** (read/search/create/update only) is the one the application actually uses; a
separate, temporary **Verifier role** holds history/vread/delete/bundle for verification only. Writes
(`DetectedIssue`/`RiskAssessment`) exist in the client and are covered by tests but are **disabled by default**
(`HEALTHLAKE_WRITE_OUTPUTS=false`) and the Lambda role has no write action at all — Phase 4 cannot write FHIR data
even by mistake.

## Actual AWS findings (not assumptions)
- `AnalyticsConfiguration.Status=DISABLED` is rejected at datastore creation; only `ENABLED`/`PAUSED` are accepted.
- Creating the datastore required Lake Formation admin/RAM probes and `glue:CreateDatabase`, because HealthLake's
  analytics integration provisions a Glue resource-link database in a HealthLake-owned account behind the scenes.
- `_revinclude` needed a documented-but-unverified spelling fallback (`patient` → `subject` → per-type searches),
  triggered only on HTTP 400, because live behavior of the parameter spelling was not fully documented.

## Consequences
- Strict validation is never weakened anywhere; a test fails the build if any script or policy requests
  `minimal`/`structure-only` validation.
- The read/write asymmetry in IAM is the actual enforcement mechanism for "no HealthLake writes in Phase 3/4" —
  not just an application-level setting, which matters because settings can be misconfigured but IAM cannot be
  bypassed by the application.
