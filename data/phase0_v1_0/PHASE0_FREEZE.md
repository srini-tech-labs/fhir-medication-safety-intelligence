# Phase 0 Freeze

Version: 1.0.0
Frozen on: 2026-09-18

## Included
- 10 synthetic patients.
- 6 easy-to-explain medication ingredients.
- 4 LOINC-coded laboratory types.
- 3 drug-drug rules.
- 4 drug-lab rules.
- 1 data-gap rule.
- 4 patients with unstructured note content (P002, P006, P008, P010).
- Golden deterministic expected results.
- FHIR R4 bundles and bulk NDJSON.
- API handoff contract.
- AI explanation guardrails.

## Change control
If a rule, threshold, RxNorm/LOINC mapping, or expected result changes, update:
1. rule/evidence files;
2. affected FHIR fixtures;
3. expected results;
4. API mocks;
5. version number.

This is demonstration data and demonstration rule logic, not a validated clinical decision-support system.
