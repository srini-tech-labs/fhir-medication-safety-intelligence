# Claude Build Brief — Phases 1, 2, and 3

Build a clean, production-shaped demo from this package. Do not redesign the clinical data contract.

## Phase 1 — Local backend
- Python backend (FastAPI is a good fit).
- Read FHIR from `fhir/bundles/`.
- Implement `ClinicalRepository`.
- Implement deterministic engine from the JSON rule files.
- Add tests for all ten golden cases.
- Implement endpoints in `api/openapi.yaml`.
- Return application JSON, not raw FHIR, to the frontend.

## Phase 2 — React frontend + AI explanation
Build a polished clinical dashboard:
- patient selector/list;
- patient summary;
- active medications;
- labs with high/low indicators;
- unstructured document viewer;
- `Analyze Medication Safety` action;
- findings grouped by HIGH/MODERATE/LOW;
- separate Data Gaps panel;
- separate AI Explanation panel;
- Evidence/Provenance panel showing rule ID, RxNorm, LOINC, and DailyMed evidence reference.

Visually separate:
1. source clinical data;
2. deterministic safety findings;
3. AI explanation.

The AI explanation service should be behind an interface and should support a mock/no-key mode.

## Phase 3 — AWS application layer, still without HealthLake
Add:
- S3 storage option;
- Lambda deployment;
- API Gateway;
- IAM least-privilege roles/policies;
- CloudWatch logging;
- environment-based repository selection.

Suggested environment variable:
`DATA_BACKEND=local|s3|healthlake`

Only implement `local` and `s3` in Phases 1–3. Leave `healthlake` as an interface/stub for Phase 4.

## Important
Do not create the AWS HealthLake datastore during Phases 1–3.
