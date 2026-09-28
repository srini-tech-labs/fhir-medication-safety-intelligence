# Application Architecture Context

## Locked architecture

Synthetic FHIR R4 data -> Local repository (Phase 1) -> S3 repository (Phase 3) -> AWS HealthLake repository (Phase 4)

Application path:
React UI -> API Gateway -> Lambda backend -> repository adapter -> FHIR data

Backend responsibilities:
- Translate FHIR resources into an application-friendly patient snapshot.
- Execute deterministic drug-drug and drug-lab rules.
- Persist DetectedIssue and RiskAssessment once HealthLake is introduced.
- Invoke an AI explanation service only after deterministic results exist.

Supporting services:
- S3: FHIR import staging, import output, and Phase 3 repository option.
- IAM: least-privilege permissions between Lambda, S3, HealthLake, and logging.
- API Gateway: stable HTTPS REST interface to the frontend.
- Lambda: application logic, rule evaluation, repository adapter, and AI orchestration.
- CloudWatch: logs, metrics, and troubleshooting.
- HealthLake: FHIR R4 datastore and FHIR REST API, introduced only in Phase 4.
- SMART-on-FHIR: separate short-lived learning lab in Phase 5.

External terminology/evidence:
- RxNorm: medication normalization/identity.
- LOINC: laboratory identity.
- DailyMed/FDA labels: rule-authoring evidence, not a required live dependency for each analysis.

## Design boundary

The React frontend MUST NOT:
- query HealthLake directly;
- parse raw FHIR;
- execute clinical rules;
- calculate severity;
- infer findings from clinical notes.

The frontend receives the application JSON contract defined in `api/openapi.yaml`.
