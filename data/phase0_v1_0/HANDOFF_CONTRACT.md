# Handover Contract for Application Build

## Source of truth
This Phase 0 package is the frozen source of truth for clinical demo data and deterministic expected outcomes.

### Claude/application code MUST use
- `data/patient_scenarios.json`
- `fhir/bundles/`
- `rules/*.json`
- `expected/expected_results.json`
- `api/openapi.yaml`
- `schemas/`
- `prompts/ai_explanation_system.txt`

## Non-negotiable constraints
1. Do not invent or modify clinical rules.
2. Do not change HIGH/MODERATE classifications without updating the Phase 0 package first.
3. Do not create findings from unstructured notes in v1.
4. Do not use the AI model to discover interactions or determine severity.
5. Do not couple the React frontend to FHIR or AWS HealthLake.
6. Do not make DailyMed a required runtime dependency for `POST /analyses`.
7. Deterministic results must match `expected/expected_results.json`.
8. P009 is the negative control and MUST return no configured clinical findings.
9. P010 is a data-gap case and MUST NOT fabricate a potassium result.
10. Every UI safety result should show rule ID, structured evidence, severity, and evidence source.

## Repository interface
Implement an abstraction equivalent to:

```python
class ClinicalRepository:
    def get_patients(self): ...
    def get_patient(self, patient_id): ...
    def get_medications(self, patient_id): ...
    def get_observations(self, patient_id): ...
    def get_documents(self, patient_id): ...
    def save_detected_issue(self, issue): ...
    def save_risk_assessment(self, assessment): ...
```

Suggested implementations over project phases:
- Phase 1: `LocalFHIRRepository`
- Phase 3: `S3FHIRRepository`
- Phase 4: `HealthLakeFHIRRepository`

Business logic MUST depend on `ClinicalRepository`, not directly on local files, boto3 S3 calls, or HealthLake.

## Deterministic analysis contract
Order:
1. Load active MedicationRequest resources.
2. Load relevant Observation resources.
3. Normalize medication and lab identifiers from FHIR coding.
4. Execute `drug_drug_rules.json`.
5. Execute `drug_lab_rules.json`.
6. Evaluate `data_gap_rules.json`.
7. Calculate overall result from deterministic findings.
8. Map each clinical finding to a FHIR DetectedIssue model.
9. Build an optional qualitative FHIR RiskAssessment roll-up.
10. Only then call the AI explanation service.

## AI boundary
The model is an explanation layer only. It may summarize supplied note context but it may not:
- add a finding;
- change severity;
- add a diagnosis;
- recommend a medication change;
- infer a missing laboratory value.

## Testing
Create automated tests that run all 10 patients and compare:
- overall severity/status;
- fired clinical rule IDs;
- data-gap rule IDs.

Do not golden-test exact AI prose.

## Version
Phase 0 contract version: 1.0.0
