# ADR-0009: Separate liveness (`/health`) from readiness (`/ready`)

**Status:** implemented and tested locally (2026-09-20); `/v1/ready` was later deployed and live-verified (2026-09-23) — see `docs/READINESS.md`.

## Context
`GET /v1/health` already existed and is part of the frozen API contract. It returns `{"status":"ok"}`
unconditionally. That is correct behavior for *liveness*, but it means nothing external is ever checked — which
became visible in this session when the HealthLake datastore was deleted (ADR-0010) and every data route started
answering 503 while `/v1/health` kept answering 200. There was no single endpoint that answered "is this instance
actually able to serve traffic."

## Decision
Add `GET /v1/ready`, deliberately not merged into `/health`. It checks exactly two **hard** dependencies —
the clinical data store and the application-state store, each via a cheap, read-only, no-PHI probe — and reports
a third, the AI explanation provider, as **informational only**: its absence never fails readiness, because
`SafeExplanationService` (ADR-0008) already degrades it gracefully. The response never contains an ARN, hostname,
resource identifier, exception message or stack trace. It is proven, not assumed, that it never invokes the model
(a test wires a provider that raises `AssertionError` if called at all).

## Consequences
- An orchestrator can now correctly distinguish "restart me" (liveness failing) from "don't route to me right now"
  (readiness failing) — exactly the two different, real conditions this project has already hit: a code bug vs.
  the HealthLake datastore being gone.
- The deployment decision (adding this to `routes.txt`/API Gateway) was deliberately left for explicit review
  rather than shipped silently, per the instruction to show the contract before deployment.
- The DynamoDB probe reuses the already-granted `GetItem` permission rather than requesting `DescribeTable`,
  keeping ADR-0004's least-privilege posture intact for an ops-only feature.
