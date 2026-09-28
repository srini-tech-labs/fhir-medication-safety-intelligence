# ADR-0007: Forced-tool structured output, independently re-validated against the original contract schema

**Status:** implemented, unit-tested with botocore `Stubber` against the real Converse service model; not yet
exercised against a real Nova response (ADR-0006).

## Context
Amazon Nova's documented structured-output mechanism (a forced tool via `toolConfig`/`toolChoice.tool`) supports
only a reduced JSON-Schema subset for a tool's *input* schema — no top-level `additionalProperties`, no `anyOf`.
The application's real explanation contract (`RESPONSE_SCHEMA`) uses both. Naively pointing the tool schema at the
full contract would either be rejected by the API or silently accept more than the contract allows.

## Decision
The Nova-facing "generation schema" is *mechanically derived* from `RESPONSE_SCHEMA` (`generation_schema()`):
`additionalProperties` is dropped everywhere, `anyOf[{type:X},{type:"null"}]` is rewritten to `type:[X,"null"]`, and
the result is asserted to use only the keys Nova documents (`type`/`properties`/`required`). This derived schema is
used **only** to shape what the model is asked to produce — it is deliberately permissive (may accept more than
the contract). The model's actual `toolUse.input` is then **re-validated against the original, stricter
`RESPONSE_SCHEMA`** by a small fail-closed validator (`schema_check.py`, proven equivalent to the `jsonschema`
library on a 4,000+ document corpus) before the response ever reaches the grounding guard (ADR-0008). A violation
here raises `ExplanationUnavailable` — the raw, invalid output is captured for review, never shown.

## Consequences
- The contract is the single source of truth; the Nova-specific schema is a derived, mechanically-checked
  artifact, not a second hand-maintained copy that could drift from it.
- A model that returns something the derived schema would accept but the real contract would not (e.g. an
  extra field) is still caught before it reaches the user — the reduction step cannot silently widen what is
  actually accepted.
- This is why moving from Sonnet 5's `outputConfig.textFormat` to Nova's forced tool (ADR-0006) required no
  change to the contract, the guard, or the fallback categories — only this one adapter-level translation.
