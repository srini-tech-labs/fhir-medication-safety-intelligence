# ADR-0004: DynamoDB for application state, default AWS-owned encryption (no KMS)

**Status:** implemented (Phase 4).

## Context
Phase 3's application state (saved analyses, rejected-explanation captures) lived on local disk, which does not
survive across Lambda containers. Phase 4 needed a shared store. The first Phase 4 plan proposed a
customer-managed KMS key for this table, matching HealthLake's CMK.

## Decision
`medsafety-app-state`: on-demand billing, partition `pk` / sort `sk`, TTL attribute `ttl` for 30-day rejected-output
captures, **DynamoDB's default AWS-owned encryption** — the user explicitly revised the plan to remove the
KMS key and every DynamoDB-related KMS permission from the Lambda role before deployment. Analyses are stored as
one opaque JSON string per item (not native maps), because DynamoDB rejects Python floats and native-map storage
would make the table's schema an unintended second copy of the API contract. Only `GetItem`/`PutItem`/`UpdateItem`/
`Query` are ever used — no `Scan`, no `Delete` — enforced both by the IAM policy and by a scope test.

## Consequences
- Simpler IAM (no CMK grant, no key-policy change) at the cost of not controlling the encryption key — an
  accepted trade for synthetic, non-sensitive demonstration data; would need revisiting for real PHI.
- The readiness probe (ADR-0009) reuses the already-granted `GetItem` action on a reserved, never-written key
  rather than requesting `DescribeTable`, keeping this least-privilege posture intact instead of quietly widening
  it for an ops feature.
- The atomic counter (`ADD n :one` on a `COUNTER` item) gives race-free analysis numbering across concurrent
  Lambda containers without a separate locking mechanism.
