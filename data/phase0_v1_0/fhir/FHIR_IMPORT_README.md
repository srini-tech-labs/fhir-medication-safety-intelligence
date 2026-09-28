# HealthLake Import Preparation

The `fhir/bulk/` directory contains newline-delimited FHIR R4 JSON, one resource per line and grouped by resource type.

Files currently generated:
- Patient.ndjson
- Encounter.ndjson
- MedicationRequest.ndjson
- Observation.ndjson
- Binary.ndjson
- DocumentReference.ndjson

Recommended Phase 4 flow:
1. Upload `fhir/bulk/` to the designated S3 HealthLake input prefix.
2. Configure a separate S3 output prefix for import results.
3. Use a HealthLake import IAM role with only required bucket/key permissions.
4. Use the chosen KMS configuration required by the import job.
5. Start the FHIR import job.
6. Inspect HealthLake `manifest.json` and SUCCESS/FAILURE outputs.
7. Query imported resources using the HealthLake FHIR REST API.
8. Do not import the example DetectedIssue/RiskAssessment as source data; those demonstrate the shape of application-generated outputs.

AWS documentation used for this preparation is listed in `SOURCES.md`.
