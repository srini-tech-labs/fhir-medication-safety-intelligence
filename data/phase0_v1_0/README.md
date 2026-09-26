# FHIR Medication Safety Intelligence — Phase 0 Package

Version **1.0.0** — frozen 2026-09-18

This ZIP is the complete Phase 0 handoff for the AWS HealthLake medication-safety prototype.

## What is included
- 10 fully synthetic patient scenarios.
- FHIR R4 `Patient`, `Encounter`, `MedicationRequest`, `Observation`, `DocumentReference`, and `Binary` resources.
- RxNorm medication mappings and LOINC laboratory mappings.
- Curated deterministic drug-drug and drug-lab rule catalog.
- DailyMed/FDA evidence manifest.
- Golden expected results for all 10 patients.
- FHIR `DetectedIssue` and `RiskAssessment` examples.
- HealthLake-ready NDJSON grouped by resource type.
- Stable application REST contract and JSON schemas.
- AI explanation guardrails.
- Claude build brief for Phases 1–3.

## Start here
1. Read `HANDOFF_CONTRACT.md`.
2. Give `CLAUDE_BUILD_BRIEF.md` plus the full ZIP to Claude.
3. Require Claude to run all golden tests in `expected/expected_results.json`.
4. Do not create AWS HealthLake yet.
5. Build local -> S3/Lambda/API Gateway first.
6. We introduce HealthLake only after the app works end to end.

## Clinical logic boundary
The deterministic engine decides:
- drug-drug findings;
- drug-lab findings;
- severity;
- data gaps.

The AI only explains those results.

Unstructured clinical notes may enrich wording but may not create a finding in v1.

## Synthetic-data guarantee
All names, identifiers, dates, notes, medications, and lab results in this package are created for demonstration. No real patient data or PHI is included.

## Safety / scope
This is an educational architecture prototype, not a validated clinical decision-support system. The project severity labels are demonstration classifications. Do not use this dataset or rule catalog for patient care.
