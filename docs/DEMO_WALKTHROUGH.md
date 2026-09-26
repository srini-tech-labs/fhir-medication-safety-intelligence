# 5-minute technical walkthrough

Everything below is runnable today, locally, at no AWS cost, and matches what has actually been built and
verified. The cloud deployment (API Gateway/Lambda/HealthLake/DynamoDB/Bedrock) is described from the real,
observed system state — the HealthLake datastore is currently **deleted** to stop billing (`docs/CHANGE_LOG.md`),
so this walkthrough demos the local stack live and shows the cloud architecture from the diagrams and reports.

## Setup (before the 5 minutes start)

```
make api     # backend/.venv/bin/uvicorn app.main:app --reload --port 8000  (DATA_BACKEND=local by default)
make ui      # frontend: npm run dev, http://localhost:5173
```

## 0:00–0:30 — Framing

"This is a deterministic medication-safety engine over synthetic FHIR R4 data, with an AI explanation layer on
top. The important design point: the rule engine decides everything — findings, severity, data gaps. The AI layer
can only restate those results in prose; it is architecturally forbidden from adding a finding or a value. If it
ever tries, or if it's unavailable, the UI shows a clearly labelled deterministic fallback instead — never a
silent guess."

## 0:30–2:00 — Live UI walkthrough (local stack)

Open http://localhost:5173. Point at the three layers on any patient card:

1. **P001** (single MODERATE drug-lab finding, `DL-001`): show Layer 1 (source data, read through the API, no FHIR
   in the browser), Layer 2 (the finding, with rule ID, evidence citation, severity), Layer 3 (the AI explanation
   panel — badge reads "Mock explanation · no model used" in local dev, since no API key is configured; it's the
   same fallback text and structure the deployed cloud API is showing right now for every patient).
2. **P006** (`DDI-003` + a data gap `DG-001`): this is the one documented, approved deviation from the frozen
   expected-results file (`docs/CHANGE_LOG.md`) — worth mentioning as an example of a real data conflict found and
   resolved with an explicit, reviewed override rather than silently edited frozen data.
3. **P008** (multiple findings, `DL-001` then `DDI-003`, verified against golden expectations and live HealthLake
   parity): shows severity-first ordering — the higher-severity finding (`DL-001`) is listed before the moderate
   one (`DDI-003`) — and that the AI panel restates *all* findings, not just one.
4. **P010** (data gap only, no findings): shows the "NEEDS_DATA" pattern — the app never estimates a missing lab
   value, it reports that the check could not run.

## 2:00–3:00 — The AI safety layer

Open `docs/adr/0008-fail-closed-grounding-guard.md` or just narrate: "Every model response is checked against the
deterministic findings it was given — medication names, doses, severities, rule IDs must all trace back. Anything
that fails that check, or any provider error, becomes this labelled fallback — never a partial or unlabelled
answer." Mention the acceptance floor (80%, Phase 2 Claude baselines were 90–94% with Sonnet/Opus on the
first-party API) and that this is a real, previously-measured number, not an assumption.

## 3:00–4:00 — The cloud path (from diagrams and reports, not a live cloud call)

Open `docs/ARCHITECTURE.md`. Walk the component diagram: React → API Gateway (IP-allowlisted, throttled,
**exactly 7 explicit routes**, not a proxy catch-all) → Lambda (the same FastAPI app, via Mangum) → HealthLake /
DynamoDB / Bedrock. Say plainly:

- HealthLake is deployed-capable but **currently deleted** to stop a ~$0.27/hour charge once its job (proving live
  parity) was done; a recorded, tested recreation workflow exists (`91_recreate.sh`, ADR-0010) and was
  deliberately not run.
- Bedrock (Amazon Nova 2 Lite, forced-tool structured output) is deployed and IAM-approved, but every real call has
  been rejected by AWS account-level checks unrelated to the code (`AccessDeniedException`, then
  `ValidationException: Operation not allowed`) — an AWS Support case is open, and no workaround or provider
  substitution was made.
- The verification suite (A0–A16) passed on everything **except** the two Bedrock-dependent checks, which are
  explicitly marked **pending**, not silently skipped or marked passed.

## 4:00–4:30 — Liveness vs readiness (new this session)

```
curl -s localhost:8000/v1/health   # {"status":"ok"} -- always, once the process is up
curl -s localhost:8000/v1/ready    # {"status":"ready","checks":{...}} -- reflects real dependency state
```

"These are deliberately different. `/health` never looks outside the process — an orchestrator uses it to decide
whether to restart. `/ready` checks the actual clinical-data and application-state stores and would have correctly
reported `not_ready` for the clinical store during the HealthLake deletion, while `/health` kept answering `ok` the
whole time — that's not hypothetical, it's what actually happened in this project."

## 4:30–5:00 — Close

"Everything deterministic is fully proven: 20/20 offline verification, 10/10 golden scenarios, live HealthLake
parity, and the full API stack live in AWS. The one thing not yet proven end-to-end is a real Nova response,
because of an AWS account restriction outside the code — and the system's own design means that gap degrades to a
clearly labelled fallback rather than a broken feature."

---

# Architect-level follow-up questions

Grounded only in what was actually built, decided, or observed — not hypothetical extensions.

1. **Grounding guard, precisely:** the guard checks that model text traces back to the deterministic analysis. What
   happens to a *correct* explanation that happens to reuse a clinical term from the patient's notes (passed as
   `note_context`) that isn't in the structured findings — is that a false-positive rejection risk, and how would
   you tell from the acceptance-rate numbers alone?
2. **Nova vs Claude baselines:** Phase 2's 80% floor and 90–94% baselines were measured against Claude Sonnet/Opus
   on the first-party API. Given Nova 2 Lite is a materially smaller/cheaper model with no prior baseline here, is
   an unchanged 80% floor the right bar, or should the first real Nova run be treated as calibration data before
   the floor is judged?
3. **The A11 concurrency finding:** a burst of ~25+ simultaneous requests produced one HTTP 500 from API Gateway
   with nothing logged on the Lambda side, while sequential throttling behaved correctly. What's your next
   concrete step to get from "reported, unconfirmed" to a root cause, given the deployer role can't read account
   Lambda concurrency settings or CloudWatch metrics?
4. **Idempotency tokens across a delete/recreate:** the recreation workflow refuses to reuse either AWS client
   token recorded for the deleted datastore/import job. Is that caution justified by anything AWS documents about
   token lifetime, or is it a defensive assumption — and how would you actually find out without risking a
   real API call?
5. **Readiness's one soft dependency:** `/v1/ready` treats the AI explanation provider as non-blocking because a
   tested fallback exists, but treats the clinical store and app state as hard failures. Is there a version of
   this system where the clinical store should *also* degrade instead of hard-failing (e.g., serving the last
   cached snapshot), and what would that change about the "never estimate a missing value" guarantee?
6. **Least-privilege readiness probe:** the DynamoDB readiness check reuses `GetItem` on a reserved key rather than
   requesting `DescribeTable`. What's the actual downside of that choice (cost, latency, blast radius) versus
   requesting the narrower-seeming but currently-unused permission?
7. **Storage-agnostic repository, cost of abstraction:** `HealthLakeFHIRRepository` and `LocalFHIRRepository`
   share one interface and are asserted to produce identical domain objects. What's a plausible future backend
   (a third `ClinicalRepository` implementation) that would strain this interface, and where specifically would
   it break first?
8. **Schema derivation risk (ADR-0007):** the Nova generation schema is mechanically derived to be a superset of
   the real contract, then the response is re-validated against the original. Can you construct a case where a
   valid derived-schema response is *semantically* wrong in a way `schema_check.py` would not catch, even though
   it's syntactically valid?
