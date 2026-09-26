# ADR-0006: Bedrock Converse + Amazon Nova 2 Lite, not the first-party Anthropic API, Bedrock Mantle, or Sonnet 5

**Status:** implemented and deployed; Nova invocation **pending AWS quota enablement** (real, observed, not
theoretical).

## Context
Phase 2 used the first-party Anthropic API directly. Phase 4 needed the explanation call to run from inside AWS,
using the Lambda's own IAM identity rather than a bearer API key. The first Phase 4 plan proposed a Bedrock Mantle
endpoint with Claude Opus 5; that was rejected in review.

## Decision, in the order it actually happened
1. **Rejected:** Bedrock Mantle / Opus 5 — replaced by Bedrock **Runtime Converse** with **Claude Sonnet 5**
   (`us.anthropic.claude-sonnet-5`), using `outputConfig.textFormat` for structured output.
2. **Preflight finding:** Sonnet 5 in this account reported `authorizationStatus: NOT_AUTHORIZED`, agreement
   `NOT_AVAILABLE`, and required Anthropic's first-time-use form — real account-level enablement, not a code
   problem. Per instruction, this path is **paused**, not worked around (no account upgrade, no Marketplace
   agreement, no form submission).
3. **Pivot, approved:** Amazon **Nova 2 Lite** (`us.amazon.nova-2-lite-v1:0`, US geo inference profile) via the
   same Converse API, using its forced-tool structured-output pattern (ADR-0007) instead of `textFormat`, with
   reasoning explicitly disabled. Read-only preflight showed the model and profile `ACTIVE`, entitlement/agreement/
   region `AVAILABLE`, no Marketplace agreement needed (Amazon models are not Marketplace products) — favorable
   compared to Sonnet 5's blocked state.
4. **Gate B finding (the actual account-level surprise):** even Nova, an Amazon-owned model with no agreement
   step, was rejected on the first real call: `AccessDeniedException` ("your account is currently being
   verified"), and on a retry ~2 hours later, `ValidationException: Operation not allowed` — an unexplained,
   account-level restriction distinct from the documented enablement paths. An AWS Support case is open.

## Consequences
- Two independent, real account-enablement blockers were hit (Anthropic's and then Nova's), on a young AWS
  account, neither predictable from documentation alone — the only way either was proven was a real call at the
  approved gate, exactly as planned (`docs/PHASE4_API.md` "Gate": "a 400 about the JSON schema, an AccessDenied,
  or a Marketplace/first-use denial → report; no silent fallback").
- The architecture, schema, guard and acceptance floor (80%) were never weakened to work around this; per
  instruction, Phase 4 shipped with the deterministic path fully verified and the AI path reported honestly as
  pending, rather than substituting a different provider to make tests pass.
- The Claude `text_format` code path (ADR-0007) was kept in the tree, tested, and paused rather than deleted, so
  resuming it later needs no re-implementation.
