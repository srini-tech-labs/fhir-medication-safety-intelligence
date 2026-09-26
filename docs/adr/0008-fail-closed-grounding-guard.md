# ADR-0008: Fail-closed grounding guard with a labelled deterministic fallback

**Status:** implemented (Phase 2), unchanged through Phase 4; exercised in production today as the *only* path
(Bedrock/Nova pending, ADR-0006).

## Context
Even a schema-valid model response (ADR-0007) could still be ungrounded prose — inventing a value, softening a
severity, or adding a recommendation the deterministic engine never made. The product's guarantee (ADR-0001) needs
that caught before display, not just schema-checked.

## Decision
Every model response passes a grounding guard that checks its text against the deterministic analysis it was
given: dates, medication names, doses, severities and rule IDs mentioned must trace back to that analysis. Any
violation raises `GroundingViolation`; `SafeExplanationService` catches **any** failure from the primary provider
— schema violation, guard rejection, refusal, timeout, or (today) a Bedrock `AccessDeniedException`/
`ValidationException` — classifies it into one of a small set of *public* fallback codes (never the raw exception
text), and returns a deterministically-templated explanation from `MockExplanationService` instead. A rejected
model completion (when one exists) is captured server-side, redacted, with a TTL, for human review — never shown
to the user and never blocking the fallback.

## Consequences
- There is no code path by which an ungrounded or unvalidated AI statement reaches the UI — degrade to the labelled
  fallback is the *only* failure behavior, by construction, not by convention.
- This is precisely why "proceed without a functioning Bedrock provider" (the actual Phase 4 instruction) was a
  coherent request rather than a broken deployment: the fallback path is a first-class, tested, always-available
  outcome, and every explanation request in production today exercises exactly this path.
- Cost: acceptance is measured against a floor (80%, Phase 2 Claude baselines 90–94%) specifically because a
  guard that is too aggressive would itself be a usability failure; the floor is a deliberate, documented,
  never-silently-lowered line (Phase 4 instruction: "do not weaken the AI acceptance criteria").
