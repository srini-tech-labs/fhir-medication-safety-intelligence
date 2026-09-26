# ADR-0011: OpenAI (`gpt-5.6-luna`) as the initial deployed cloud explanation provider

**Status:** implemented and deployed, live-verified 2026-09-23 — see `docs/PHASE5_OPENAI_DEPLOYMENT.md` for the
executed command record and results.

## Context
Phase 4 originally targeted Bedrock/Nova as the cloud explanation provider (ADR-0006), but that path has
remained blocked since implementation on an open AWS Support case for Bedrock model-access enablement in this
account, with no resolution date. Meanwhile, a local, offline evaluation harness (`docs/PROVIDER_EVALUATION.md`)
compared four independent explanation providers — Anthropic direct, OpenAI, Google Gemini, and Databricks
Foundation Model API — behind the unchanged `ExplanationService` abstraction, running the same frozen P001–P010
golden corpus through the same, unmodified grounding guard and `SafeExplanationService`. That evaluation is now
complete and frozen: no further live provider calls or benchmark tuning are planned. OpenAI's results were clean
(schema success, guard acceptance, and fallback rate all at the same bar the Bedrock/Nova plan required) and its
SDK is already a proven, lazily-imported dependency pattern in this codebase (`services/explanation/openai.py`).

The application cannot stay indefinitely without *any* deployed cloud explanation provider — the deterministic
engine remains fully functional and is the sole clinical authority either way, but the AI-explanation feature of
the deployed API is otherwise permanently degraded to the mock/fallback path.

## Decision
Deploy OpenAI (`gpt-5.6-luna`) as the **initial** cloud explanation provider for the Lambda-backed API, entirely
through configuration:
- `EXPLANATION_PROVIDER=openai`, `EXPLANATION_MODEL=gpt-5.6-luna` (Lambda environment variables, not code).
- The production OpenAI API key is stored in AWS Secrets Manager as its own secret (`medsafety/openai-api-key`)
  and never appears as a literal Lambda environment variable, in the deployment output, in logs, or in the
  Lambda package. `MedSafetyApiLambdaRole` is granted a single, least-privilege `secretsmanager:GetSecretValue`
  statement scoped to exactly that one secret ARN (`infrastructure/aws/api/policies/lambda-secrets-openai.json.tpl`)
  — no wildcard resource, no access to any other secret.
- `backend/app/secrets.py` fetches the secret once, at construction time (Lambda cold start), and writes it into
  the same `OPENAI_API_KEY` environment variable the `openai` SDK already reads — so it flows through the
  existing `redact.py` exact-value redaction with no new redaction path to maintain, and the provider module
  itself (`services/explanation/openai.py`) needed zero changes to consume it.
- `scripts/build_lambda.py` gained a `--provider` flag; the OpenAI package bundles the `openai` SDK and *only*
  the `openai` SDK (Anthropic and Google SDKs remain forbidden in every package, unconditionally, per the
  existing `verify_zip()` checks).
- Nothing about the *application's* architecture changes: `ExplanationService` stays a one-method abstraction,
  `SafeExplanationService` still wraps exactly one primary provider plus the deterministic mock fallback, the
  grounding guard and `RESPONSE_SCHEMA` are provider-independent and untouched, and `/v1/ready` still reports
  `explanationProvider` as a construction-time status without ever invoking the model or depending on it for
  the readiness verdict (ADR-0009 is unaffected).

Bedrock/Nova is **not removed**. `EXPLANATION_PROVIDER=bedrock` remains fully implemented, selectable, and
tested — it is simply not the provider chosen to deploy first, given the ongoing account blocker. Google Gemini
and Databricks remain evaluation-only candidates (ADR non-goal): nothing about this decision proposes deploying
either of them, and no automatic failover to any of the three non-selected providers is implemented or planned —
the only failure path, exactly as before, is the deterministic `MockExplanationService`.

## Consequences
- The deployed API gets a working AI-explanation path without waiting on the Bedrock Support case, at the cost
  of introducing a new external dependency (OpenAI availability/pricing) and one new AWS resource type
  (Secrets Manager) to the account.
- Switching providers later (back to Bedrock once unblocked, or to a different evaluated provider) remains a
  configuration change — new secret/IAM grant if credential-based, `EXPLANATION_PROVIDER`/`EXPLANATION_MODEL`
  env vars, and a `--provider`-flagged rebuild — never an application code change, which is the reason the
  `ExplanationService` abstraction (ADR-0001) exists in the first place.
- Secrets Manager adds a small recurring cost (~$0.40/month/secret plus negligible API-call cost) and a new
  IAM surface, scoped to the minimum: one role, one action, one resource.
- The deterministic engine remains the sole clinical authority regardless of which cloud provider is deployed
  or whether it is reachable at all — this decision only ever affects explanatory text, never a finding.
