#!/usr/bin/env python
"""Opt-in live validation for Anthropic / OpenAI / Gemini / Databricks, using the exact same corpus and check
groups as scripts/live_claude_check.py (see live_check_lib.py). NOT part of `make test`.
scripts/live_claude_check.py itself is left completely untouched -- it remains the trusted, single-purpose,
already-proven Anthropic path (and its own Makefile targets are unaffected); `--provider anthropic` here
is an additional, unified way to reach Anthropic through the same ProviderContext/report/comparison path as
the other three providers, using the same ClaudeExplanationService.

    put ANTHROPIC_API_KEY=... / OPENAI_API_KEY=... / GEMINI_API_KEY=... / DATABRICKS_HOST=...+DATABRICKS_TOKEN=...
    in ./.local/providers.env
    backend/.venv/bin/python scripts/live_check.py --provider anthropic --out live_report_anthropic.json
    backend/.venv/bin/python scripts/live_check.py --provider openai --selftest   # fake client, no key, no network

Credentials load from .local/providers.env (+ .local/databricks.cfg fallback for Databricks) via the same
allow-listed loader scripts/live_claude_check.py uses for .env -- never printed, never written to the report.
The real global ~/.databrickscfg is never read. This script makes NO real API call by itself; it only runs
when invoked, and --selftest never touches a network or a real credential.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "backend"))

import live_check_lib as lib  # noqa: E402

from app.config import Settings  # noqa: E402
from app.container import build_container  # noqa: E402
from app.envfile import load_databricks_cfg, load_env_file  # noqa: E402
from app.redact import install_log_redaction  # noqa: E402
from app.services.explanation.factory import SafeExplanationService  # noqa: E402
from app.services.explanation.mock import MockExplanationService  # noqa: E402
from app.terminology import Terminology  # noqa: E402

LOCAL_PROVIDERS_ENV = REPO / ".local" / "providers.env"
LOCAL_DATABRICKS_CFG = REPO / ".local" / "databricks.cfg"

BOGUS = {"anthropic": "sk-ant-api03-INVALID-KEY-FOR-LIVE-ERROR-TEST-0000000000",
        "openai": "sk-proj-INVALID-KEY-FOR-LIVE-ERROR-TEST-0000000000",
        "gemini": "AIzaINVALIDKEYFORLIVEERRORTEST0000000",
        "databricks": "dapi-INVALID-TOKEN-FOR-LIVE-ERROR-TEST-000000"}
CREDENTIAL_ENV = {"anthropic": ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"), "openai": ("OPENAI_API_KEY",),
                  "gemini": ("GEMINI_API_KEY",), "databricks": ("DATABRICKS_HOST", "DATABRICKS_TOKEN")}
ALL_OF = {"databricks"}  # the rest are any-of (any single credential in CREDENTIAL_ENV[provider] suffices)
PRICES = {  # $/MTok in,out -- human-supplied; update as pricing changes, "n/a" otherwise
    "claude-opus-5": (5.0, 25.0), "claude-sonnet-5": (2.0, 10.0), "claude-haiku-4-5": (1.0, 5.0),
    "gpt-5.6-luna": (None, None),
    "gemini-3.8-flash": (None, None),
}
SDK_LOGGER_NAMES = {"anthropic": ("anthropic", "httpx", "httpx2", "httpcore"),
                    "openai": ("openai", "httpx", "httpx2", "httpcore"),
                    "gemini": ("google_genai", "google.genai", "httpx", "httpx2", "httpcore"),
                    "databricks": ("openai", "httpx", "httpx2", "httpcore")}


# ---- recording wrappers: one per client shape, all normalise into the same calls[] record ------------------------
class RecordingMessagesClient:
    """Wraps an Anthropic-compatible client's .messages.create (same shape as scripts/live_claude_check.py's
    own RecordingClient, ported here so it normalises into live_check_lib's shared calls[] record)."""

    def __init__(self, inner, label, calls):
        self._inner, self._label, self.calls = inner, label, calls
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kw):
        started = time.perf_counter()
        try:
            resp = self._inner.messages.create(**kw)
        except Exception as exc:
            self.calls.append({"label": self._label, "model": kw.get("model"), "error": type(exc).__name__,
                               "http_status": getattr(exc, "status_code", None), "seconds": round(time.perf_counter() - started, 2)})
            raise
        u = resp.usage
        self.calls.append({"label": self._label, "model": resp.model, "stop_reason": resp.stop_reason,
                           "input_tokens": u.input_tokens, "output_tokens": u.output_tokens,
                           "cache_read_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
                           "cache_write_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0,
                           "request_id": getattr(resp, "_request_id", None), "seconds": round(time.perf_counter() - started, 2)})
        return resp


class RecordingResponsesClient:
    def __init__(self, inner, label, calls):
        self._inner, self._label, self.calls = inner, label, calls
        self.responses = SimpleNamespace(create=self._create)

    def _create(self, **kw):
        started = time.perf_counter()
        try:
            resp = self._inner.responses.create(**kw)
        except Exception as exc:
            self.calls.append({"label": self._label, "model": kw.get("model"), "error": type(exc).__name__,
                               "http_status": getattr(exc, "status_code", None), "seconds": round(time.perf_counter() - started, 2)})
            raise
        u = getattr(resp, "usage", None)
        cached = getattr(getattr(u, "input_tokens_details", None), "cached_tokens", 0) if u else 0
        self.calls.append({"label": self._label, "model": getattr(resp, "model", kw.get("model")),
                           "stop_reason": getattr(resp, "status", None),
                           "input_tokens": getattr(u, "input_tokens", 0) or 0, "output_tokens": getattr(u, "output_tokens", 0) or 0,
                           "cache_read_tokens": cached or 0, "cache_write_tokens": 0,
                           "request_id": getattr(resp, "id", None), "seconds": round(time.perf_counter() - started, 2)})
        return resp


class RecordingChatClient:
    def __init__(self, inner, label, calls):
        self._inner, self._label, self.calls = inner, label, calls
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        started = time.perf_counter()
        try:
            resp = self._inner.chat.completions.create(**kw)
        except Exception as exc:
            self.calls.append({"label": self._label, "model": kw.get("model"), "error": type(exc).__name__,
                               "http_status": getattr(exc, "status_code", None), "seconds": round(time.perf_counter() - started, 2)})
            raise
        u = getattr(resp, "usage", None)
        self.calls.append({"label": self._label, "model": getattr(resp, "model", kw.get("model")),
                           "stop_reason": resp.choices[0].finish_reason,
                           "input_tokens": getattr(u, "prompt_tokens", 0) or 0, "output_tokens": getattr(u, "completion_tokens", 0) or 0,
                           "cache_read_tokens": 0, "cache_write_tokens": 0,
                           "request_id": getattr(resp, "id", None), "seconds": round(time.perf_counter() - started, 2)})
        return resp


class RecordingGeminiClient:
    def __init__(self, inner, label, calls):
        self._inner, self._label, self.calls = inner, label, calls
        self.models = SimpleNamespace(generate_content=self._create)

    def _create(self, **kw):
        started = time.perf_counter()
        try:
            resp = self._inner.models.generate_content(**kw)
        except Exception as exc:
            self.calls.append({"label": self._label, "model": kw.get("model"), "error": type(exc).__name__,
                               "http_status": getattr(exc, "status_code", None), "seconds": round(time.perf_counter() - started, 2)})
            raise
        u = getattr(resp, "usage_metadata", None)
        candidates = getattr(resp, "candidates", None) or []
        finish = getattr(candidates[0], "finish_reason", None) if candidates else None
        self.calls.append({"label": self._label, "model": getattr(resp, "model_version", kw.get("model")),
                           "stop_reason": getattr(finish, "name", finish),
                           "input_tokens": getattr(u, "prompt_token_count", 0) or 0,
                           "output_tokens": getattr(u, "candidates_token_count", 0) or 0,
                           "cache_read_tokens": 0, "cache_write_tokens": 0,
                           "request_id": None, "seconds": round(time.perf_counter() - started, 2)})
        return resp


# ---- --selftest fakes: well-behaved except when an injection tells them to misbehave, or `kind` says otherwise ---
def fake_anthropic_messages(kind: str, bogus: str):
    """Same behaviour as scripts/live_claude_check.py's own FakeAnthropic, ported here."""
    def create(**kw):
        if kind == "bogus":
            raise RuntimeError(f"401 authentication_error x-api-key: {bogus}")
        if kw["model"].startswith("does-not-exist"):
            raise LookupError("404 not_found_error: model: does-not-exist")
        mi = json.loads(kw["messages"][0]["content"])
        payload = _selftest_payload(mi)
        usage = SimpleNamespace(input_tokens=1200, output_tokens=350, cache_read_input_tokens=0, cache_creation_input_tokens=0)
        stop_reason = "max_tokens" if kw["max_tokens"] < 100 else "end_turn"
        return SimpleNamespace(stop_reason=stop_reason, model=kw["model"], usage=usage, _request_id="req_selftest",
                               content=[SimpleNamespace(type="text", text=json.dumps(payload))])
    return SimpleNamespace(messages=SimpleNamespace(create=create))


def fake_openai_responses(kind: str, bogus: str):
    def create(**kw):
        if kind == "bogus":
            raise RuntimeError(f"401 authentication_error x-api-key: {bogus}")
        if kw["model"].startswith("does-not-exist"):
            raise LookupError("404 not_found_error: model: does-not-exist")
        mi = json.loads(kw["input"])
        payload = _selftest_payload(mi)
        incomplete = kw.get("max_output_tokens", 999) < 100
        details = SimpleNamespace(reason="max_output_tokens") if incomplete else None
        content = [SimpleNamespace(type="output_text", text=json.dumps(payload))]
        return SimpleNamespace(status="incomplete" if incomplete else "completed", model=kw["model"], id="resp_selftest",
                               output=[SimpleNamespace(content=content)],
                               usage=SimpleNamespace(input_tokens=1200, output_tokens=350,
                                                     input_tokens_details=SimpleNamespace(cached_tokens=0)),
                               incomplete_details=details)
    return SimpleNamespace(responses=SimpleNamespace(create=create))


def fake_chat_completions(kind: str, bogus: str):
    def create(**kw):
        if kind == "bogus":
            raise RuntimeError(f"401 authentication_error x-api-key: {bogus}")
        if kw["model"].startswith("does-not-exist"):
            raise LookupError("404 not_found_error: model: does-not-exist")
        # The user message may be prefixed with a short "Return only valid JSON..." instruction ahead of the
        # JSON payload (DatabricksExplanationService._user_prompt(), json_object mode) -- parse from the
        # first '{' so this fake accepts both the prefixed and unprefixed (prompt_only) shape.
        content = kw["messages"][-1]["content"]
        mi = json.loads(content[content.index("{"):])
        payload = _selftest_payload(mi)
        finish = "length" if kw.get("max_tokens", 999) < 100 else "stop"
        message = SimpleNamespace(content=json.dumps(payload))
        choice = SimpleNamespace(finish_reason=finish, message=message)
        return SimpleNamespace(choices=[choice], model=kw["model"], id="chatcmpl_selftest",
                               usage=SimpleNamespace(prompt_tokens=1200, completion_tokens=350))
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def fake_gemini(kind: str, bogus: str):
    def create(**kw):
        if kind == "bogus":
            raise RuntimeError(f"401 UNAUTHENTICATED api key: {bogus}")
        if kw["model"].startswith("does-not-exist"):
            raise LookupError("404 NOT_FOUND: model does-not-exist")
        mi = json.loads(kw["contents"])
        payload = _selftest_payload(mi)
        finish_name = "MAX_TOKENS" if kw.get("config", {}).get("max_output_tokens", 999) < 100 else "STOP"
        candidate = SimpleNamespace(finish_reason=SimpleNamespace(name=finish_name))
        return SimpleNamespace(candidates=[candidate], model_version=kw["model"], text=json.dumps(payload),
                               usage_metadata=SimpleNamespace(prompt_token_count=1200, candidates_token_count=350))
    return SimpleNamespace(models=SimpleNamespace(generate_content=create))


def _selftest_payload(mi: dict) -> dict:
    ctx = mi.get("unstructuredContext") or ""
    expl = [{"ruleId": f["ruleId"], "explanation": f"{f['ruleId']} ({f['severity']}): {f['finding']}"}
           for f in mi["deterministicFindings"]]
    if "DDI-009" in ctx:  # simulate a model that obeys the injected note
        expl.append({"ruleId": "DDI-009", "explanation": "Warfarin with lisinopril is HIGH severity; discontinue lisinopril."})
    return {"summary": f"The rule engine reported {mi['summaryCounts']['totalFindings']} findings and "
                       f"{mi['summaryCounts']['dataGaps']} data gaps.",
           "findingExplanations": expl, "dataGapExplanation": " ".join(g["finding"] for g in mi["dataGaps"]) or None,
           "groundedInFindingsOnly": True}


# ---- per-provider ProviderContext construction --------------------------------------------------------------
def build_anthropic_context(base: Settings, args, calls, rejections) -> lib.ProviderContext:
    from app.services.explanation.claude import ClaudeExplanationService

    pkg, terms, model = base.package_dir, Terminology.load(base.package_dir), base.explanation_model
    tmp = _tmp_root("anthropic")

    def make(label, *, model_name=None, kind="real", max_tokens=16000, sub="x"):
        if args.selftest:
            raw = fake_anthropic_messages(kind, BOGUS["anthropic"])
        else:
            import anthropic

            # Explicit, not `anthropic.Anthropic()` with no args: read the loaded credential out of os.environ
            # (main() populated it from .local/providers.env) and pass it in, rather than trusting the SDK's own
            # implicit lookup -- the same principle as the OpenAI/Gemini/Databricks branches below, and the fix
            # applied to check_provider_availability.py after the credential-flow bug found there. api_key takes
            # precedence over auth_token when both are set, matching factory.py's any-of order.
            if kind == "bogus":
                raw = anthropic.Anthropic(api_key=BOGUS["anthropic"])
            elif os.getenv("ANTHROPIC_API_KEY"):
                raw = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
            else:
                raw = anthropic.Anthropic(auth_token=os.getenv("ANTHROPIC_AUTH_TOKEN"))
        rec = RecordingMessagesClient(raw, label, calls)
        svc = ClaudeExplanationService(pkg, terms, model_name or model, client=rec, max_tokens=max_tokens)
        settings = Settings(**{**base.__dict__, "output_dir": tmp / sub, "explanation_mode": "claude"})
        return build_container(settings, explainer=SafeExplanationService(svc, MockExplanationService(), on_rejection=rejections.append))

    baseline = build_container(Settings(**{**base.__dict__, "output_dir": tmp / "baseline", "explanation_mode": "mock"}),
                               explainer=MockExplanationService())
    normal = make("normal", sub="normal")
    degraded = [
        lib.DegradedCase("bad key (real 401)", make("bad-key", kind="bogus", sub="c1"), "EXPLANATION_UNAVAILABLE"),
        lib.DegradedCase("unknown model (real 404)", make("bad-model", model_name="does-not-exist-000", sub="c2"), "EXPLANATION_UNAVAILABLE"),
        lib.DegradedCase("truncated output", make("truncated", max_tokens=32, sub="c3"), "EXPLANATION_INCOMPLETE"),
    ]
    return lib.ProviderContext("anthropic", model, pkg, terms, baseline, normal, degraded, calls, rejections,
                               _current_secrets("anthropic"), BOGUS["anthropic"], PRICES, SDK_LOGGER_NAMES["anthropic"], args.selftest)


def build_openai_context(base: Settings, args, calls, rejections) -> lib.ProviderContext:
    from app.services.explanation.openai import OpenAIExplanationService

    pkg, terms, model = base.package_dir, Terminology.load(base.package_dir), base.explanation_model
    tmp = _tmp_root("openai")

    def make(label, *, model_name=None, kind="real", max_tokens=16000, sub="x"):
        if args.selftest:
            raw = fake_openai_responses(kind, BOGUS["openai"])
        else:
            import openai

            # Explicit, not `openai.OpenAI()` with no args: main() already loaded OPENAI_API_KEY into os.environ
            # from .local/providers.env, but the credential is still read out and passed explicitly here rather
            # than trusting the SDK's own implicit env lookup (see check_provider_availability.py's fix for why).
            raw = openai.OpenAI(api_key=BOGUS["openai"] if kind == "bogus" else os.getenv("OPENAI_API_KEY"))
        rec = RecordingResponsesClient(raw, label, calls)
        svc = OpenAIExplanationService(pkg, terms, model_name or model, client=rec, max_tokens=max_tokens)
        settings = Settings(**{**base.__dict__, "output_dir": tmp / sub, "explanation_mode": "claude"})
        return build_container(settings, explainer=SafeExplanationService(svc, MockExplanationService(), on_rejection=rejections.append))

    baseline = build_container(Settings(**{**base.__dict__, "output_dir": tmp / "baseline", "explanation_mode": "mock"}),
                               explainer=MockExplanationService())
    normal = make("normal", sub="normal")
    degraded = [
        lib.DegradedCase("bad key (real 401)", make("bad-key", kind="bogus", sub="c1"), "EXPLANATION_UNAVAILABLE"),
        lib.DegradedCase("unknown model (real 404)", make("bad-model", model_name="does-not-exist-000", sub="c2"), "EXPLANATION_UNAVAILABLE"),
        lib.DegradedCase("truncated output", make("truncated", max_tokens=32, sub="c3"), "EXPLANATION_INCOMPLETE"),
    ]
    return lib.ProviderContext("openai", model, pkg, terms, baseline, normal, degraded, calls, rejections,
                               _current_secrets("openai"), BOGUS["openai"], PRICES, SDK_LOGGER_NAMES["openai"], args.selftest)


def build_databricks_context(base: Settings, args, calls, rejections) -> lib.ProviderContext:
    from app.services.explanation.databricks import DatabricksExplanationService

    pkg, terms, endpoint = base.package_dir, Terminology.load(base.package_dir), base.databricks_endpoint
    host, token = base.databricks_host, os.getenv("DATABRICKS_TOKEN")
    tmp = _tmp_root("databricks")

    def make(label, *, model_name=None, kind="real", max_tokens=4000, sub="x"):
        if args.selftest:
            raw = fake_chat_completions(kind, BOGUS["databricks"])
        else:
            import openai

            raw = openai.OpenAI(base_url=f"{host}/serving-endpoints", api_key=BOGUS["databricks"] if kind == "bogus" else token)
        rec = RecordingChatClient(raw, label, calls)
        svc = DatabricksExplanationService(pkg, terms, model_name or endpoint, host=host, token=token, client=rec, max_tokens=max_tokens)
        settings = Settings(**{**base.__dict__, "output_dir": tmp / sub, "explanation_mode": "claude"})
        return build_container(settings, explainer=SafeExplanationService(svc, MockExplanationService(), on_rejection=rejections.append))

    baseline = build_container(Settings(**{**base.__dict__, "output_dir": tmp / "baseline", "explanation_mode": "mock"}),
                               explainer=MockExplanationService())
    normal = make("normal", sub="normal")
    degraded = [
        lib.DegradedCase("bad token (real 401)", make("bad-token", kind="bogus", sub="c1"), "EXPLANATION_UNAVAILABLE"),
        lib.DegradedCase("unknown endpoint (real 404)", make("bad-endpoint", model_name="does-not-exist-000", sub="c2"), "EXPLANATION_UNAVAILABLE"),
        lib.DegradedCase("truncated output", make("truncated", max_tokens=32, sub="c3"), "EXPLANATION_INCOMPLETE"),
    ]
    return lib.ProviderContext("databricks", endpoint, pkg, terms, baseline, normal, degraded, calls, rejections,
                               _current_secrets("databricks"), BOGUS["databricks"], PRICES, SDK_LOGGER_NAMES["databricks"], args.selftest)


def build_gemini_context(base: Settings, args, calls, rejections) -> lib.ProviderContext:
    from app.services.explanation.gemini import GeminiExplanationService

    pkg, terms, model = base.package_dir, Terminology.load(base.package_dir), base.explanation_model
    tmp = _tmp_root("gemini")

    def make(label, *, model_name=None, kind="real", max_tokens=4000, sub="x"):
        if args.selftest:
            raw = fake_gemini(kind, BOGUS["gemini"])
        else:
            from google import genai

            # Explicit, not `genai.Client()` with no args -- same reasoning as OpenAI's branch above.
            raw = genai.Client(api_key=BOGUS["gemini"] if kind == "bogus" else os.getenv("GEMINI_API_KEY"))
        rec = RecordingGeminiClient(raw, label, calls)
        svc = GeminiExplanationService(pkg, terms, model_name or model, client=rec, max_tokens=max_tokens)
        settings = Settings(**{**base.__dict__, "output_dir": tmp / sub, "explanation_mode": "claude"})
        return build_container(settings, explainer=SafeExplanationService(svc, MockExplanationService(), on_rejection=rejections.append))

    baseline = build_container(Settings(**{**base.__dict__, "output_dir": tmp / "baseline", "explanation_mode": "mock"}),
                               explainer=MockExplanationService())
    normal = make("normal", sub="normal")
    degraded = [
        lib.DegradedCase("bad key (real 401)", make("bad-key", kind="bogus", sub="c1"), "EXPLANATION_UNAVAILABLE"),
        lib.DegradedCase("unknown model (real 404)", make("bad-model", model_name="does-not-exist-000", sub="c2"), "EXPLANATION_UNAVAILABLE"),
        lib.DegradedCase("truncated output", make("truncated", max_tokens=32, sub="c3"), "EXPLANATION_INCOMPLETE"),
    ]
    return lib.ProviderContext("gemini", model, pkg, terms, baseline, normal, degraded, calls, rejections,
                               _current_secrets("gemini"), BOGUS["gemini"], PRICES, SDK_LOGGER_NAMES["gemini"], args.selftest)


BUILDERS = {"anthropic": build_anthropic_context, "openai": build_openai_context, "gemini": build_gemini_context,
           "databricks": build_databricks_context}
DEFAULT_MODEL = {"anthropic": "claude-opus-5", "openai": "gpt-5.6-luna", "gemini": "gemini-3.8-flash", "databricks": None}


def _tmp_root(provider: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=f"live-{provider}-check-"))


def _current_secrets(provider: str) -> list[str]:
    return [v for name in CREDENTIAL_ENV[provider] if (v := os.getenv(name))]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", required=True, choices=sorted(BUILDERS))
    ap.add_argument("--model", default=None)
    ap.add_argument("--selftest", action="store_true", help="fake client; no key, no network")
    ap.add_argument("--patients", default=lib.DEFAULT_PATIENTS)
    ap.add_argument("--out", default=None, help="write the JSON report here")
    args = ap.parse_args()

    if not args.selftest:  # self-test must never touch real credentials
        loaded = load_env_file(LOCAL_PROVIDERS_ENV)  # names only; values are never printed
        if loaded:
            print(f"loaded from .local/providers.env: {', '.join(loaded)}")
        if args.provider == "databricks":
            loaded_cfg = load_databricks_cfg(LOCAL_DATABRICKS_CFG)  # only fills gaps left by env/providers.env
            if loaded_cfg:
                print(f"loaded from .local/databricks.cfg: {', '.join(loaded_cfg)}")
    if not args.selftest and not _current_secrets(args.provider):
        joiner = " + " if args.provider in ALL_OF else " or "
        names = joiner.join(CREDENTIAL_ENV[args.provider])
        print(f"No {names} found. Put it/them in .local/providers.env or export them, then re-run "
             f"(or use --selftest for a no-network dry run).", file=sys.stderr)
        return 2

    install_log_redaction()  # what create_app() does at server start: pins SDK loggers + record backstop
    base = Settings.from_env()
    overrides = {"explanation_provider": args.provider}
    if args.provider == "databricks":
        # for Databricks, "--model" names the serving endpoint (there is no separate model id to pick)
        if args.model:
            overrides["databricks_endpoint"] = args.model
    else:
        overrides["explanation_model"] = args.model or DEFAULT_MODEL[args.provider]
    base = Settings(**{**base.__dict__, **overrides})

    calls: list[dict] = []
    rejections: list[dict] = []
    ctx = BUILDERS[args.provider](base, args, calls, rejections)
    patients = [p.strip() for p in args.patients.split(",") if p.strip()]
    report = lib.run(ctx, patients)
    return lib.write_report(report, [BOGUS[args.provider], *_current_secrets(args.provider)], args.out)


if __name__ == "__main__":
    raise SystemExit(main())
