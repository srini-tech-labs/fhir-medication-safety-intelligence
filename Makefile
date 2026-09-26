.PHONY: aws-api-deployer-policy build-lambda api ui test test-backend test-frontend live-check live-check-sonnet live-compare live-selftest aws-deployer-policy aws-preflight test-healthlake-live check-providers live-check-anthropic live-check-openai live-check-gemini live-check-databricks live-selftest-anthropic live-selftest-openai live-selftest-gemini live-selftest-databricks live-compare-all

api:
	cd backend && .venv/bin/uvicorn app.main:app --reload --port 8000

ui:
	cd frontend && npm run dev

test: test-backend test-frontend

test-backend:
	backend/.venv/bin/python -m pytest

test-frontend:
	cd frontend && npm run typecheck && npm test

# Opt-in, calls the real Claude API (needs ANTHROPIC_API_KEY in the environment; ~8 small calls).
live-check:
	backend/.venv/bin/python scripts/live_claude_check.py --out live_report.json

# Same harness and cases on Sonnet; keeps the Opus baseline (live_report_opus.json) for comparison. ~$0.06.
live-check-sonnet:
	EXPLANATION_MODEL=claude-sonnet-5 backend/.venv/bin/python scripts/live_claude_check.py --out live_report_sonnet.json

live-compare:
	backend/.venv/bin/python scripts/compare_live_reports.py live_report_opus.json live_report_sonnet.json

live-selftest:
	env -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN backend/.venv/bin/python scripts/live_claude_check.py --selftest

# ---- Unified multi-provider evaluation harness (Anthropic / OpenAI / Gemini / Databricks), one ProviderContext,
# report format, check groups, acceptance logic and comparison path for all four. scripts/live_claude_check.py
# (targets above) remains the separate, untouched, already-proven Anthropic-only path -- `live-check-anthropic`
# below is an ADDITIONAL way to reach Anthropic through this unified script, not a replacement.
# Credentials load from .local/providers.env (git-ignored; see .local.example/). OpenAI/Gemini/Databricks need
# `pip install -e "backend[eval]"`; Anthropic's SDK is already a core dependency.
# Read-only, zero-cost by default: reports which providers are actually available. Pass --verify to this target's
# script directly for an opt-in, real, billed check (never run automatically).
check-providers:
	backend/.venv/bin/python scripts/check_provider_availability.py

# Opt-in, calls the real Anthropic/OpenAI/Gemini/Databricks APIs (needs credentials in .local/providers.env).
live-check-anthropic:
	backend/.venv/bin/python scripts/live_check.py --provider anthropic --out live_report_anthropic.json

live-check-openai:
	backend/.venv/bin/python scripts/live_check.py --provider openai --out live_report_openai.json

live-check-gemini:
	backend/.venv/bin/python scripts/live_check.py --provider gemini --out live_report_gemini.json

live-check-databricks:
	backend/.venv/bin/python scripts/live_check.py --provider databricks --out live_report_databricks.json

live-selftest-anthropic:
	env -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN backend/.venv/bin/python scripts/live_check.py --provider anthropic --selftest

live-selftest-openai:
	env -u OPENAI_API_KEY backend/.venv/bin/python scripts/live_check.py --provider openai --selftest

live-selftest-gemini:
	env -u GEMINI_API_KEY backend/.venv/bin/python scripts/live_check.py --provider gemini --selftest

live-selftest-databricks:
	env -u DATABRICKS_HOST -u DATABRICKS_TOKEN backend/.venv/bin/python scripts/live_check.py --provider databricks --selftest

live-compare-all:
	backend/.venv/bin/python scripts/compare_live_reports_nway.py live_report_anthropic.json live_report_openai.json live_report_gemini.json live_report_databricks.json

# ---- Phase 3 (AWS HealthLake). Nothing here creates resources except via the numbered scripts you run explicitly. ----
# Render the least-privilege deployer policy to attach to YOUR AWS identity:  make aws-deployer-policy ACCT=123456789012
aws-deployer-policy:
	@test -n "$(ACCT)" || { echo "usage: make aws-deployer-policy ACCT=<12-digit account id>"; exit 1; }
	@ACCT=$(ACCT) IMPORT_BUCKET=medsafety-hl-import-$(ACCT)-us-east-1 RESULTS_BUCKET=medsafety-hl-results-$(ACCT)-us-east-1 \
	  python3 infrastructure/aws/healthlake/render_policy.py infrastructure/aws/healthlake/policies/deployer-policy.json.tpl

# READ-ONLY AWS preflight (needs AWS_PROFILE).
aws-preflight:
	bash infrastructure/aws/healthlake/00_preflight.sh

# Opt-in live parity test against a real datastore (needs HEALTHLAKE_DATASTORE_ID, HEALTHLAKE_ROLE_ARN, AWS_PROFILE).
test-healthlake-live:
	RUN_HEALTHLAKE_LIVE=1 backend/.venv/bin/python -m pytest -m live_aws tests/live_aws -o addopts="-q"

# ---- Phase 4 (API Gateway + Lambda + Bedrock). Nothing here creates AWS resources. ----
# Render the ADDITIVE Phase 4 deployer policy for review:  make aws-api-deployer-policy ACCT=123456789012
aws-api-deployer-policy:
	@test -n "$(ACCT)" || { echo "usage: make aws-api-deployer-policy ACCT=<12-digit account id>"; exit 1; }
	@ACCT=$(ACCT) python3 infrastructure/aws/healthlake/render_policy.py infrastructure/aws/api/policies/deployer-policy-phase4.json.tpl

# Build the arm64 Lambda zip locally (no AWS): frozen data checksum-verified, boto3 pinned, no Anthropic SDK.
build-lambda:
	backend/.venv/bin/python scripts/build_lambda.py --out build/medsafety-api.zip

