#!/usr/bin/env python
"""Read-only, zero-cost by default: which explanation providers are actually usable right now.

    backend/.venv/bin/python scripts/check_provider_availability.py
    backend/.venv/bin/python scripts/check_provider_availability.py --verify openai,gemini   # opt-in, costs a real call each

Tier 1 (always, free, no network): credential PRESENCE only -- env / .local/providers.env / .local/databricks.cfg
/ absent -- and whether each provider's optional SDK package is installed. Never prints a credential value.
Gemini is never inferred from a Google/Gemini account or subscription -- only a real GEMINI_API_KEY counts.
Databricks never reads the unrelated global ~/.databrickscfg -- only .local/databricks.cfg (project-local).

Tier 2 (--verify, opt-in, never automatic): one minimal real call per requested provider to confirm the
credential actually authenticates. For Databricks this instead lists serving endpoints (GET, no inference
call) so you can see what's actually provisioned before choosing DATABRICKS_ENDPOINT.

This script never provisions, purchases or enables anything. If nothing is available, it says so and exits 0
(this is a report, not a gate) unless --require names providers that must be present.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "backend"))

from app.envfile import load_databricks_cfg, load_env_file  # noqa: E402

LOCAL_PROVIDERS_ENV = REPO / ".local" / "providers.env"
LOCAL_DATABRICKS_CFG = REPO / ".local" / "databricks.cfg"
GLOBAL_DATABRICKS_CFG = Path.home() / ".databrickscfg"

PROVIDERS = {
    "anthropic": {"env": ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"), "all_of": False, "package": "anthropic"},
    "openai": {"env": ("OPENAI_API_KEY",), "all_of": False, "package": "openai"},
    "gemini": {"env": ("GEMINI_API_KEY",), "all_of": False, "package": "google.genai"},
    "databricks": {"env": ("DATABRICKS_HOST", "DATABRICKS_TOKEN"), "all_of": True, "package": "openai"},
}


def package_installed(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except ModuleNotFoundError:
        return False


def presence(provider: str, env: dict) -> dict:
    spec = PROVIDERS[provider]
    have = {name: bool(env.get(name)) for name in spec["env"]}
    present = all(have.values()) if spec["all_of"] else any(have.values())
    return {"provider": provider, "credential_present": present, "credential_detail": have,
           "package_installed": package_installed(spec["package"])}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify", default="", help="comma-separated providers to make one real, minimal call against (costs money/quota)")
    ap.add_argument("--require", default="", help="comma-separated providers that must be present; exit 1 otherwise")
    args = ap.parse_args()

    # Deliberately loaded into a private local dict, NEVER into os.environ: `_verify()` below passes every
    # credential explicitly into its SDK client constructor (api_key=..., auth_token=...), so no SDK's own
    # implicit os.environ lookup is ever relied on, and nothing here needs a persistent (or even process-lifetime)
    # environment variable. (A prior version of this script loaded into this same local dict but then let
    # `_verify()` construct SDK clients with zero arguments -- relying on implicit os.environ discovery that
    # this dict never fed -- which silently failed with e.g. Gemini's "No API key was provided". Fixed.)
    env: dict[str, str] = {}
    loaded = load_env_file(LOCAL_PROVIDERS_ENV, env)
    if loaded:
        print(f"loaded from .local/providers.env: {', '.join(loaded)}")
    loaded_cfg = load_databricks_cfg(LOCAL_DATABRICKS_CFG, env)
    if loaded_cfg:
        print(f"loaded from .local/databricks.cfg: {', '.join(loaded_cfg)}")
    if GLOBAL_DATABRICKS_CFG.is_file():
        print(f"note: {GLOBAL_DATABRICKS_CFG} exists but is NOT used (project-local .local/databricks.cfg only, by policy)")

    # env/.local/providers.env precedence already resolved by load_env_file (existing os.environ wins); merge for reporting
    full_env = {**env, **{name: os.environ[name] for spec in PROVIDERS.values() for name in spec["env"] if name in os.environ}}

    print(f"\n{'provider':<12} {'credential':<10} {'package':<10}  detail")
    rows = []
    for provider in PROVIDERS:
        row = presence(provider, full_env)
        rows.append(row)
        cred = "present" if row["credential_present"] else "ABSENT"
        pkg = "installed" if row["package_installed"] else "missing"
        detail = ", ".join(f"{k}={'set' if v else 'unset'}" for k, v in row["credential_detail"].items())
        print(f"{provider:<12} {cred:<10} {pkg:<10}  {detail}")
    print("\nGemini availability is gated strictly on a real GEMINI_API_KEY -- never assumed from a Google/Gemini "
         "subscription. Bedrock/Nova is out of scope (pending AWS Support) and not checked here.")

    if args.verify:
        print("\n--verify requested: this WILL make a real, minimal, billed call per named provider. Not run automatically.")
        for provider in [p.strip() for p in args.verify.split(",") if p.strip()]:
            _verify(provider, full_env)

    if args.require:
        missing = [p for p in (p.strip() for p in args.require.split(",")) if p and not next(r for r in rows if r["provider"] == p)["credential_present"]]
        if missing:
            print(f"\nSTOP: required provider(s) not available: {', '.join(missing)}", file=sys.stderr)
            return 1
    return 0


def _verify(provider: str, env: dict) -> None:
    """One minimal real call (or, for Databricks, a read-only endpoint listing) to confirm a credential works.
    Deliberately NOT wired into the default (no-flag) path -- opt-in only, per provider, on explicit request.
    Never raises: prints PASS/FAIL and the (redacted) reason."""
    if provider not in PROVIDERS:
        print(f"  {provider}: unknown provider, skipping")
        return
    if not presence(provider, env)["credential_present"]:
        print(f"  {provider}: not available, skipped")
        return
    from app.redact import redact

    try:
        if provider == "anthropic":
            import anthropic

            # api_key takes precedence, same any-of order as everywhere else in this codebase
            client = anthropic.Anthropic(api_key=env["ANTHROPIC_API_KEY"]) if env.get("ANTHROPIC_API_KEY") \
                else anthropic.Anthropic(auth_token=env["ANTHROPIC_AUTH_TOKEN"])
            r = client.messages.create(model="claude-haiku-4-5", max_tokens=8, messages=[{"role": "user", "content": "ping"}])
            print(f"  {provider}: PASS (model={r.model})")
        elif provider == "openai":
            import openai

            # Bind to a local first: `openai.OpenAI(...).responses.create(...)` chained in one expression would
            # let the constructed client's refcount drop to zero (nothing else references it) the moment
            # `.responses` is accessed, which can trigger client teardown before the call completes -- exactly
            # the bug this comment is here to prevent (see the `gemini` branch below, where it actually happened).
            client = openai.OpenAI(api_key=env["OPENAI_API_KEY"])
            # max_output_tokens has a documented floor of 16 on the Responses API; 8 was rejected with a 400
            # ("integer below minimum value... Expected a value >= 16") on the first real --verify openai run.
            r = client.responses.create(model="gpt-5.6-luna", input="ping", max_output_tokens=16)
            print(f"  {provider}: PASS (model={getattr(r, 'model', '?')})")
        elif provider == "gemini":
            from google import genai

            # Bind to a local first -- do not chain `genai.Client(...).models.generate_content(...)`. google-genai's
            # `Client.__del__` closes its underlying httpx client; chained, the temporary `Client` had nothing
            # else referencing it, so CPython garbage-collected (and closed) it right as the request was about to
            # fire, raising "Cannot send a request, as the client has been closed." -- not a credential problem,
            # found on the first real --verify gemini run. Keeping `client` alive for the whole call fixes it.
            client = genai.Client(api_key=env["GEMINI_API_KEY"])
            r = client.models.generate_content(model="gemini-3.8-flash", contents="ping")
            print(f"  {provider}: PASS (model={getattr(r, 'model_version', '?')})")
        elif provider == "databricks":
            import json
            import urllib.request

            req = urllib.request.Request(f"{env['DATABRICKS_HOST']}/api/2.0/serving-endpoints",
                                         headers={"Authorization": f"Bearer {env['DATABRICKS_TOKEN']}"})
            with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 - fixed https workspace host, read-only GET
                endpoints = [e["name"] for e in json.loads(resp.read()).get("endpoints", [])]
            print(f"  {provider}: PASS ({len(endpoints)} serving endpoint(s) provisioned: {endpoints[:10]})")
    except Exception as exc:  # noqa: BLE001 - report, never crash the availability check
        print(f"  {provider}: FAIL ({redact(f'{type(exc).__name__}: {exc}')[:200]})")


if __name__ == "__main__":
    raise SystemExit(main())
