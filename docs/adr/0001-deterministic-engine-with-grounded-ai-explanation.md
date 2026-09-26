# ADR-0001: Deterministic rule engine decides; AI only explains, never decides

**Status:** implemented (Phase 1), unchanged through Phase 4.

## Context
This is a medication-safety tool over synthetic data. A wrong or hallucinated finding is the worst possible
failure mode for this domain. An LLM can be fluent and still be wrong about dosages, interactions or thresholds.

## Decision
All findings, severities and data gaps are produced by a deterministic rule engine (`backend/app/rules/`) evaluating
structured FHIR-derived data against versioned rule definitions. The AI explanation layer runs strictly *after* the
engine, receives only the engine's own output as input, and is architecturally forbidden from adding a finding,
changing a severity, inferring a missing value, or making a treatment recommendation. A fail-closed grounding guard
(ADR-0008) enforces this on every model response before it can reach the user.

## Consequences
- The product's safety-critical guarantee does not depend on model behavior, model choice, or model availability.
- This is why the system works, and was demonstrated end to end, even while the AI provider was completely
  unavailable (Bedrock/Nova pending AWS quota enablement) — the deterministic path and its 20/20 golden and V0–V19
  verification never depended on it.
- Cost: the AI layer can only restate, never add value beyond explanation; this is a deliberate trade against
  richer AI-driven analysis, made because this is a safety tool over synthetic demonstration data, not a
  production clinical decision system.
