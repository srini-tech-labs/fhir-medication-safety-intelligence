# Readiness (`GET /v1/ready`)

**Status: deployed and live-verified (2026-09-23).** `/v1/ready` is in `infrastructure/aws/api/routes.txt` (8
contract routes now) and answers through the real API Gateway with the same source-IP allowlist as every other
route: `200 {"status":"ready","checks":{"clinicalStore":"ok","appState":"ok","explanationProvider":"configured"}}`
while HealthLake is ACTIVE.

## Why a second endpoint, not just `/v1/health`

`/v1/health` (unchanged) is **liveness**: is the process itself up and able to answer at all. It checks nothing
external and always returns `{"status": "ok"}` once the app has started. An orchestrator uses liveness to decide
whether to **restart** an instance.

`/v1/ready` is **readiness**: can this instance *usefully serve traffic right now*. It checks the backing stores
the app actually depends on. An orchestrator uses readiness to decide whether to **route traffic** to an instance,
without restarting it — the process is fine, a dependency is temporarily down.

Conflating the two is a common mistake: if `/health` also checked the clinical data store, a HealthLake or
DynamoDB blip would make an orchestrator repeatedly kill and restart a perfectly healthy Lambda/container, which
fixes nothing and adds cold starts on top of the outage. Keeping them separate is exactly what let us keep
`/v1/health` returning `200` through the HealthLake deletion in this session, while `/v1/ready` correctly reports
the clinical store as unavailable.

## Contract

```
GET /v1/ready
```

| Status | Body |
|---|---|
| `200` | `{"status": "ready", "checks": {"clinicalStore": "ok", "appState": "ok", "explanationProvider": "configured"}}` |
| `503` | `{"status": "not_ready", "checks": {"clinicalStore": "unavailable", "appState": "ok", "explanationProvider": "configured"}}` |

- `checks.clinicalStore`: `"ok"` \| `"unavailable"` — the clinical data store answered a cheap, read-only,
  no-PHI reachability probe (a FHIR capability-statement read; trivially `"ok"` for the local/file backend, which
  has no external dependency).
- `checks.appState`: `"ok"` \| `"unavailable"` — the application-state store answered a cheap, read-only probe
  (a `GetItem` on a reserved key that is never written; trivially `"ok"` for the local backend).
- `checks.explanationProvider`: `"configured"` \| `"not_configured"` — whether a real AI-explanation provider is
  wired at all (as opposed to the deterministic-only mock). **This is a construction check, not a live-invocation
  check: it never calls the model.**
- **Overall `status`/HTTP code is driven only by `clinicalStore` and `appState`.** `explanationProvider` is
  reported but never fails readiness — see the design decision below.
- The body **never** contains an ARN, hostname, table/datastore identifier, exception type or message, or stack
  trace. Those stay in the redacted server log, exactly like every other endpoint's error handling.

## Design decisions worth stating explicitly

1. **`explanationProvider` is informational, not a readiness gate.** The explanation endpoint already has a
   tested, labelled fallback (`EXPLANATION_UNAVAILABLE`) for any provider failure — the deterministic findings,
   which are the product's core guarantee, are never affected. Failing readiness over an AI-provider outage would
   take an otherwise fully-functional instance out of rotation for no benefit. Live-confirmed: `explanationProvider`
   reports `"configured"` and a real OpenAI call has succeeded end to end, but `/v1/ready`'s status code has never
   depended on that — it was already proven to report `"ready"` correctly during the window when the AI provider
   was unreachable (Bedrock, pending AWS quota) and clinicalStore was the one legitimately failing check (HealthLake
   deleted, 2026-09-19–2026-09-23 — see `docs/CHANGE_LOG.md`).
2. **One IAM change was needed, found only by deploying this for real.** The application-state probe reuses
   `dynamodb:GetItem`, which the Lambda role already had — no change there. But the clinical-store probe (`GET
   metadata`, the FHIR capability statement) needs `healthlake:GetCapabilities`, a distinct action from
   `ReadResource`/`SearchWithGet` that `BaseAccess` had never been granted — every offline test stayed green
   because `tests/support/fake_healthlake.py`'s permission model already (correctly) required it, but the real
   deployed IAM template had drifted. Fixed in `infrastructure/aws/api/policies/lambda-access.json.tpl`, applied
   live via `20_role.sh`, and now covered by a structural regression test
   (`test_lambda_role_grants_every_healthlake_action_the_offline_fake_requires_of_the_app_role`) tying the
   template to the fake so this can't silently drift again. `dynamodb:DescribeTable` is still deliberately not
   granted; the appState probe doesn't need it.
3. **Never invokes the model.** Proven directly in tests (`test_ready_never_invokes_the_explanation_provider`), not
   just inferred from the status code, by wiring a provider that raises if `.explain()` is ever called, and
   live-confirmed: the 200 response above was captured without a `configured` explanation provider ever being
   called.
4. **No internal details leaked.** Proven directly in tests (`test_ready_never_leaks_an_arn_hostname_datastore_id_or_exception_text`)
   and confirmed against the live response body.
