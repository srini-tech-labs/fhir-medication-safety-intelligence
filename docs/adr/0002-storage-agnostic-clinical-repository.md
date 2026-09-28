# ADR-0002: One `ClinicalRepository` interface behind local and cloud backends

**Status:** implemented (Phase 1), extended in Phase 3, unchanged in Phase 4.

## Context
The project needed to run against local frozen FHIR bundles (fast, offline, deterministic tests) and later against
a real cloud FHIR store (AWS HealthLake), with identical rule-engine behavior in both cases. Business logic must
not know or care which one is behind it.

## Decision
`ClinicalRepository` (`backend/app/repository/base.py`) is an abstract interface (patients, medications,
observations, documents, save/read analysis, save-rejected-explanation). `LocalFHIRRepository` /
`FHIRBundleRepository` implement it over the frozen Phase 0 package; `HealthLakeFHIRRepository` implements it over
AWS HealthLake, reusing the *same* `parse_bundle()` so both backends produce byte-identical domain objects from
equivalent FHIR data. Selection is by `DATA_BACKEND` (`local` | `healthlake`); only these two values are valid — a
scope test (`test_scope.py`) pins this and forbids stray AWS-SDK references outside the adapter files.

## Consequences
- Parity is provable, not assumed: golden-scenario tests run through both backends and are asserted equal for all
  ten patients (P006 override, P008 multi-finding order, P009 negative control, P010 data-gap case included).
- Adding readiness (ADR-0009) only required two small additions to this interface (`ping`, `ping_app_state`) with
  safe do-nothing defaults — the local backend needed zero new code.
- The interface also made the Phase 4 recreation workflow (ADR-0010) reason about swapping the wired HealthLake
  client without touching a single line of `rules/`, `services/`, or `api/`.
