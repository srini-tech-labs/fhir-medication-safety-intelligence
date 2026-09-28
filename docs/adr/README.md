# Architecture decision records

Concise ADRs for the major decisions and actual AWS findings across Phases 1–4. Each is deliberately short:
context, decision, consequences. They record *why*, not implementation detail (that lives in `docs/CHANGE_LOG.md`,
`docs/PHASE3_HEALTHLAKE.md`, `docs/PHASE4_API.md` and the code itself).

| ADR | Title | Phase |
|---|---|---|
| [0001](0001-deterministic-engine-with-grounded-ai-explanation.md) | Deterministic rule engine decides; AI only explains, never decides | 1 |
| [0002](0002-storage-agnostic-clinical-repository.md) | One `ClinicalRepository` interface behind local and cloud backends | 1–3 |
| [0003](0003-healthlake-as-cloud-fhir-store.md) | AWS HealthLake as the cloud FHIR system of record, read-mostly | 3 |
| [0004](0004-dynamodb-app-state-default-encryption.md) | DynamoDB for application state, default AWS-owned encryption (no KMS) | 4 |
| [0005](0005-lambda-api-gateway-explicit-routes.md) | Lambda + API Gateway with explicit routes and an IP allowlist, not proxy+/Cognito | 4 |
| [0006](0006-bedrock-nova-over-anthropic-and-mantle.md) | Bedrock Converse + Amazon Nova 2 Lite, not the first-party Anthropic API, Bedrock Mantle, or Sonnet 5 | 2, 4 |
| [0007](0007-forced-tool-schema-with-independent-revalidation.md) | Forced-tool structured output, independently re-validated against the original contract schema | 4 |
| [0008](0008-fail-closed-grounding-guard.md) | Fail-closed grounding guard with a labelled deterministic fallback | 2, 4 |
| [0009](0009-liveness-readiness-separation.md) | Separate liveness (`/health`) from readiness (`/ready`) | 4 |
| [0010](0010-healthlake-deletion-with-recorded-recreation.md) | Delete the HealthLake datastore to stop billing, with a recorded and tested recreation workflow | 3–4 |
