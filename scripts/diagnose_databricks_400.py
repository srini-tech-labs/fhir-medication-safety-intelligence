#!/usr/bin/env python
"""Opt-in, three-step diagnostic to isolate the exact cause of the HTTP 400 databricks-gpt-oss-120b returned
on EVERY normal production request in the first live Databricks run -- while the intentional bad-token and
bad-endpoint cases correctly got 403/404, proving the endpoint/auth path itself is valid. NOT part of make
test. Does not touch RESPONSE_SCHEMA, guard.py, SafeExplanationService, acceptance thresholds, or any other
provider -- this only ever constructs raw chat-completion requests directly, never through those layers.

    backend/.venv/bin/python scripts/diagnose_databricks_400.py

Three escalating steps, run in order, STOPPING at the first failure:
  A. bare chat completion: one simple user message, max_tokens=256, reasoning_effort="low",
     no response_format, no medication-safety payload.
  B. same as A, + response_format={"type": "json_object"}.
  C. same as B, but the messages are the REAL system instruction (data/phase0_v1_0/prompts/
     ai_explanation_system.txt) and a REAL model input (built via guard.build_model_input against the
     frozen, already-public P008 golden patient) -- i.e. byte-for-byte what
     DatabricksExplanationService._complete() actually sends in production, in json_object mode.

Isolates whether the 400 is caused by: a generic request option (fails at A), a GPT-OSS reasoning_effort
requirement (also fails at A -- the two are not separable by this script alone; see the printed guidance if
A fails), structured output (fails at B, not A), or the production payload itself (fails at C, not B).

Only the SANITIZED Databricks error code/http status/message is ever recorded or printed -- never request
headers, the token, patient data, or the full prompt/response content. A successful step prints only its
token-usage counts, never the response text (step C's response would be a real medication-safety
explanation). Credentials load via the same .local/providers.env project-local strategy scripts/live_check.py
uses (process env is an optional override; the real global ~/.databrickscfg is never read).

This script MAKES REAL, BILLED CALLS when run for real. It is being prepared, reviewed and unit-tested with
a fake client in this pass; it is NOT executed against the real API until that review is explicit.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "backend"))

from app.config import Settings  # noqa: E402
from app.envfile import load_databricks_cfg, load_env_file  # noqa: E402
from app.redact import redact  # noqa: E402
from app.services.explanation.databricks import DatabricksExplanationService  # noqa: E402
from app.services.explanation.guard import build_model_input  # noqa: E402
from app.terminology import Terminology  # noqa: E402

LOCAL_PROVIDERS_ENV = REPO / ".local" / "providers.env"
LOCAL_DATABRICKS_CFG = REPO / ".local" / "databricks.cfg"
DEFAULT_ENDPOINT = "databricks-gpt-oss-120b"
MAX_TOKENS = 256
REASONING_EFFORT = "low"


def sanitized_error(exc: Exception) -> dict:
    """Only the Databricks/OpenAI-compatible error code, http status and message -- never headers, the
    token, patient data or the full prompt. `body` (when present) is the SDK's already-parsed JSON error
    body, which for Databricks/OpenAI-compatible endpoints is `{"error_code"/"code": ..., "message": ...}`;
    it does not include the request that was sent."""
    body = getattr(exc, "body", None)
    code = None
    message = None
    if isinstance(body, dict):
        code = body.get("error_code") or body.get("code") or body.get("type")
        message = body.get("message")
    if message is None:
        message = str(exc)
    return {"exception_type": type(exc).__name__, "http_status": getattr(exc, "status_code", None),
           "error_code": code, "message": redact(str(message))[:500]}


def summarize_success(response) -> dict:
    """Only token-usage counts and the finish reason -- never the response content."""
    usage = getattr(response, "usage", None)
    choice = response.choices[0] if getattr(response, "choices", None) else None
    return {"finish_reason": getattr(choice, "finish_reason", None) if choice else None,
           "prompt_tokens": getattr(usage, "prompt_tokens", None) if usage else None,
           "completion_tokens": getattr(usage, "completion_tokens", None) if usage else None}


def step(client, label: str, **kwargs) -> tuple[bool, dict]:
    print(f"\n--- step {label} ---")
    try:
        response = client.chat.completions.create(**kwargs)
    except Exception as exc:  # noqa: BLE001 - reported, sanitized, never raised raw
        err = sanitized_error(exc)
        print(f"  FAIL: {err}")
        return False, err
    ok = summarize_success(response)
    print(f"  PASS: {ok}")
    return True, ok


def build_client(host: str, token: str):
    import openai

    return openai.OpenAI(base_url=f"{host}/serving-endpoints", api_key=token)


def build_steps(endpoint: str, package_dir, terms, model_input: dict) -> list[tuple[str, dict]]:
    # Step C's system/user message content is built via the REAL DatabricksExplanationService methods (a
    # throwaway instance; host/token are never used since no client is constructed here), so this always
    # matches production exactly -- including the json-mention fix in _system_prompt()/_user_prompt(), so a
    # re-run of this script verifies the fix rather than re-discovering the same 400.
    provider = DatabricksExplanationService(package_dir, terms, endpoint, host="unused", token="unused")
    return [
        ("A: bare request, no response_format, no production payload",
         dict(model=endpoint, max_tokens=MAX_TOKENS, reasoning_effort=REASONING_EFFORT,
              messages=[{"role": "user", "content": "Say hello in one short sentence."}])),
        ("B: A + response_format=json_object",
         dict(model=endpoint, max_tokens=MAX_TOKENS, reasoning_effort=REASONING_EFFORT,
              response_format={"type": "json_object"},
              messages=[{"role": "user", "content": "Say hello in one short sentence, as JSON: {\"greeting\": \"...\"}"}])),
        ("C: B + the real system instruction and model input (matches RESPONSE_SCHEMA's contract shape)",
         dict(model=endpoint, max_tokens=MAX_TOKENS, reasoning_effort=REASONING_EFFORT,
              response_format={"type": "json_object"},
              messages=[{"role": "system", "content": provider._system_prompt()},
                        {"role": "user", "content": provider._user_prompt(model_input)}])),
    ]


def run_diagnostic(client, steps: list[tuple[str, dict]]) -> list[tuple[str, bool, dict]]:
    """Runs `steps` in order against `client`, stopping at the first failure. Fully offline/testable: the
    caller supplies the client (real or fake) and the already-built step list."""
    results: list[tuple[str, bool, dict]] = []
    for label, kwargs in steps:
        ok, detail = step(client, label, **kwargs)
        results.append((label, ok, detail))
        if not ok:
            print(f"\nSTOPPED at step {label!r}. See the sanitized error above.")
            if label.startswith("A"):
                print("Step A itself failed: this points at a generic request option or the GPT-OSS "
                     "reasoning_effort requirement (this script alone cannot separate the two -- re-run "
                     "manually without reasoning_effort to isolate further, reviewing first).")
            break
    else:
        print("\nAll three steps PASSED. The endpoint accepts the real production payload; the reported 400 "
             "does not reproduce with this exact shape -- re-check for a transient/account-side cause.")
    print(f"\nSummary: {[(label, ok) for label, ok, _ in results]}")
    return results


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    ap.add_argument("--execute", action="store_true",
                    help="actually make the real, billed call(s). Without this flag, the script only prints "
                         "the exact request shape for each step and exits -- a dry run, no network at all.")
    args = ap.parse_args()

    if not args.execute:
        print("DRY RUN (no --execute given): printing the three request shapes only, no network call made.\n")

    import os

    host = token = None
    if args.execute:  # dry-run needs no credentials at all -- it never touches the network
        loaded = load_env_file(LOCAL_PROVIDERS_ENV)
        if loaded:
            print(f"loaded from .local/providers.env: {', '.join(loaded)}")
        loaded_cfg = load_databricks_cfg(LOCAL_DATABRICKS_CFG)
        if loaded_cfg:
            print(f"loaded from .local/databricks.cfg: {', '.join(loaded_cfg)}")
        host, token = os.getenv("DATABRICKS_HOST"), os.getenv("DATABRICKS_TOKEN")
        if not host or not token:
            print("No DATABRICKS_HOST + DATABRICKS_TOKEN found. Put both in .local/providers.env, then re-run.", file=sys.stderr)
            return 2

    settings = Settings.from_env()
    terms = Terminology.load(settings.package_dir)

    # Build the REAL model input the same way DatabricksExplanationService._complete() would, without going
    # through the guard or SafeExplanationService -- this script never runs the full explain() pipeline, only
    # constructs the same raw chat-completion request kwargs (via the provider's own prompt-building methods,
    # see build_steps(), so step C is always byte-for-byte identical to what production actually sends).
    from app.container import build_container
    from app.services.explanation.mock import MockExplanationService

    mock_settings = Settings(**{**settings.__dict__, "explanation_mode": "mock"})
    container = build_container(mock_settings, explainer=MockExplanationService())
    analysis = container.analyses.run("P008").model_copy(update={"ai_explanation": None})
    snapshot = container.snapshots.snapshot("P008")
    model_input = build_model_input(snapshot, analysis, None, terms)
    steps = build_steps(args.endpoint, settings.package_dir, terms, model_input)

    if not args.execute:
        for label, kwargs in steps:
            shape = {k: (f"<{len(v)} messages, {sum(len(m['content']) for m in v)} chars>" if k == "messages" else v)
                    for k, v in kwargs.items()}
            print(f"\n--- step {label} (not sent) ---\n  {shape}")
        print("\nRe-run with --execute to actually make these calls (real, billed, stops at the first failure).")
        return 0

    client = build_client(host, token)
    results = run_diagnostic(client, steps)
    return 0 if results and results[-1][1] else 1


if __name__ == "__main__":
    raise SystemExit(main())
