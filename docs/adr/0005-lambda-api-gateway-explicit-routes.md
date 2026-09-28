# ADR-0005: Lambda + API Gateway with explicit routes and an IP allowlist, not proxy+/Cognito

**Status:** implemented and deployed (Phase 4).

## Context
The existing FastAPI app needed to run in AWS in front of the React UI, for a single-developer prototype over
synthetic data, without standing up authentication infrastructure out of scope for this project (SMART on FHIR,
Cognito — explicitly excluded).

## Decision
The FastAPI app runs unchanged inside Lambda via the Mangum ASGI adapter (`python3.12`, `arm64`, one function).
API Gateway (REST, regional) declares **exactly the app's own contract routes** (`routes.txt`, asserted equal to
`app.openapi()`'s paths by a test) rather than a single `{proxy+}` catch-all — this means `/docs`, `/openapi.json`
and `/redoc` are structurally inaccessible, not just undocumented. Access control is a source-IP resource policy
(single `/32`, one operator) plus per-stage and per-route throttling (5 rps/burst 10; 2 rps/burst 3 on the
explanation route specifically, to cap AI spend), instead of an identity provider.

## Actual AWS findings
- Lambda memory is capped at **512 MB** on this (young) account; the plan called for 1024 MB. Size-only effect,
  no IAM or architecture change; the deployed function uses at most ~131 MB.
- API Gateway stage throttling via CLI shorthand broke on the literal braces in `{patient_id}`; fixed by patching
  through a JSON file (`--patch-operations file://...`) instead of inline shorthand.
- A concurrent burst of ~25+ simultaneous requests produced one observed HTTP 500 from API Gateway with no
  corresponding Lambda error — sustained throttling (429, never 5xx) was verified separately and works; the burst
  case is most likely this account's Lambda concurrency limit, unconfirmed (the deployer role cannot read account
  concurrency settings or CloudWatch metrics). Reported as an open finding, not silently accepted.

## Consequences
- The security model is appropriate for a single-operator, IP-known prototype over synthetic data and explicitly
  not appropriate as-is for multi-user or real-PHI use — that would need SMART/Cognito, which is out of scope by
  design, not by oversight.
- Explicit routes make the deployed surface exactly auditable against the app's own contract, which is what the
  test enforces on every change (it is also how `/v1/ready`, ADR-0009, was kept implemented-but-undeployed).
