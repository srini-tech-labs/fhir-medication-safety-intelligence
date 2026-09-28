"""Regression: scripts/live_check.py's "real" (non-selftest) client construction must pass the credential
explicitly into the SDK constructor, not rely on `openai.OpenAI()`/`genai.Client()` with no arguments.

Unlike check_provider_availability.py's bug, live_check.py's main() does load .local/providers.env into
os.environ (necessary: Settings.from_env() reads DATABRICKS_HOST/DATABRICKS_ENDPOINT that way) -- so the
SDK's own implicit lookup happened to still work here. But relying on that is exactly the pattern that broke
check_provider_availability.py, so these prove the explicit `api_key=os.getenv(...)` fix is what actually
puts the value on the wire, not an incidental SDK-side env lookup.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from types import SimpleNamespace

import pytest

from app.config import REPO_ROOT, Settings

sys.path.insert(0, str(REPO_ROOT / "scripts"))  # live_check.py imports its sibling live_check_lib.py by bare name
spec = importlib.util.spec_from_file_location("live_check", REPO_ROOT / "scripts" / "live_check.py")
live_check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(live_check)

import live_check_lib  # noqa: E402
from tests.conftest import PACKAGE_DIR  # noqa: E402

ONLY_LOCAL_ANTHROPIC_KEY = "sk-ant-api03-ONLY-SET-BY-THIS-TEST-NOT-A-REAL-EXPORT"
ONLY_LOCAL_OPENAI_KEY = "sk-proj-ONLY-SET-BY-THIS-TEST-NOT-A-REAL-EXPORT"
ONLY_LOCAL_GEMINI_KEY = "AIzaONLYSETBYTHISTESTNOTAREALEXPORT000"


def base_settings(tmp_path) -> Settings:
    b = Settings.from_env()
    return Settings(**{**b.__dict__, "package_dir": PACKAGE_DIR, "output_dir": tmp_path})


def test_cli_accepts_provider_anthropic():
    """The approved plan specified `--provider {anthropic,openai,gemini,databricks}`; anthropic was missing
    from BUILDERS until this fix. Proves the CLI choice set now matches the plan exactly."""
    assert set(live_check.BUILDERS) == {"anthropic", "openai", "gemini", "databricks"}
    proc = subprocess.run([sys.executable, str(REPO_ROOT / "scripts" / "live_check.py"), "--help"],
                          capture_output=True, text=True, cwd=REPO_ROOT / "backend")
    assert proc.returncode == 0 and "anthropic" in proc.stdout


def test_anthropic_context_real_path_passes_the_env_credential_explicitly(tmp_path, monkeypatch):
    """The exact regression requested: a credential that exists only via .local/providers.env (simulated here
    by monkeypatch.setenv, exactly what main() does after loading that file) must reach anthropic.Anthropic()
    as an explicit api_key kwarg -- not via the SDK's own implicit os.environ discovery."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", ONLY_LOCAL_ANTHROPIC_KEY)  # simulates main() having loaded .local/providers.env
    captured = []

    class FakeAnthropic:
        def __init__(self, **kw):
            captured.append(kw)
            self.messages = SimpleNamespace(create=lambda **_: SimpleNamespace(model="claude-opus-5"))

    import anthropic

    monkeypatch.setattr(anthropic, "Anthropic", FakeAnthropic)
    args = SimpleNamespace(selftest=False, model=None)
    live_check.build_anthropic_context(base_settings(tmp_path), args, [], [])
    real_calls = [kw for kw in captured if kw.get("api_key") != live_check.BOGUS["anthropic"]]
    assert real_calls and all(kw.get("api_key") == ONLY_LOCAL_ANTHROPIC_KEY for kw in real_calls)


def test_anthropic_context_falls_back_to_auth_token_explicitly(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "only-set-by-this-test-token")
    captured = []

    class FakeAnthropic:
        def __init__(self, **kw):
            captured.append(kw)
            self.messages = SimpleNamespace(create=lambda **_: SimpleNamespace(model="claude-opus-5"))

    import anthropic

    monkeypatch.setattr(anthropic, "Anthropic", FakeAnthropic)
    args = SimpleNamespace(selftest=False, model=None)
    live_check.build_anthropic_context(base_settings(tmp_path), args, [], [])
    real_calls = [kw for kw in captured if kw.get("api_key") != live_check.BOGUS["anthropic"]]
    assert real_calls and all(kw.get("auth_token") == "only-set-by-this-test-token" for kw in real_calls)


def test_anthropic_uses_the_same_provider_context_report_shape_as_the_others(tmp_path):
    """Same ProviderContext, checks, price table and SDK-logger set as openai/gemini/databricks -- no
    Anthropic-specific report shape."""
    args = SimpleNamespace(selftest=True, model=None)
    ctx = live_check.build_anthropic_context(base_settings(tmp_path), args, [], [])
    assert isinstance(ctx, live_check_lib.ProviderContext)
    assert ctx.name == "anthropic" and ctx.model == "claude-opus-5"
    assert len(ctx.degraded_cases) == 3  # same group-C shape as the other three providers


def test_openai_context_real_path_passes_the_env_credential_explicitly(tmp_path, monkeypatch):
    pytest.importorskip("openai")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", ONLY_LOCAL_OPENAI_KEY)  # simulates main() having loaded .local/providers.env
    captured = []

    class FakeOpenAI:
        def __init__(self, **kw):
            captured.append(kw)

    import openai

    monkeypatch.setattr(openai, "OpenAI", FakeOpenAI)
    args = SimpleNamespace(selftest=False, model=None)
    live_check.build_openai_context(base_settings(tmp_path), args, [], [])
    real_calls = [kw for kw in captured if kw.get("api_key") != live_check.BOGUS["openai"]]
    assert real_calls and all(kw["api_key"] == ONLY_LOCAL_OPENAI_KEY for kw in real_calls)


def test_gemini_context_real_path_passes_the_env_credential_explicitly(tmp_path, monkeypatch):
    pytest.importorskip("google.genai")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", ONLY_LOCAL_GEMINI_KEY)  # simulates main() having loaded .local/providers.env
    captured = []

    class FakeClient:
        def __init__(self, **kw):
            captured.append(kw)

    from google import genai

    monkeypatch.setattr(genai, "Client", FakeClient)
    args = SimpleNamespace(selftest=False, model=None)
    live_check.build_gemini_context(base_settings(tmp_path), args, [], [])
    real_calls = [kw for kw in captured if kw.get("api_key") != live_check.BOGUS["gemini"]]
    assert real_calls and all(kw["api_key"] == ONLY_LOCAL_GEMINI_KEY for kw in real_calls)


def test_databricks_context_already_passes_host_and_token_explicitly(tmp_path, monkeypatch):
    """Preserved, not changed by this fix -- confirms the pre-existing explicit host/token flow still holds."""
    pytest.importorskip("openai")
    monkeypatch.setenv("DATABRICKS_HOST", "https://example.cloud.databricks.com")
    monkeypatch.setenv("DATABRICKS_TOKEN", "only-set-by-this-test")
    monkeypatch.setenv("DATABRICKS_ENDPOINT", "probe-endpoint")
    captured = []

    class FakeOpenAI:
        def __init__(self, **kw):
            captured.append(kw)

    import openai

    monkeypatch.setattr(openai, "OpenAI", FakeOpenAI)
    args = SimpleNamespace(selftest=False, model=None)
    base = Settings.from_env()
    base = Settings(**{**base.__dict__, "package_dir": PACKAGE_DIR, "output_dir": tmp_path,
                       "databricks_host": "https://example.cloud.databricks.com", "databricks_endpoint": "probe-endpoint"})
    live_check.build_databricks_context(base, args, [], [])
    real_calls = [kw for kw in captured if kw.get("api_key") != live_check.BOGUS["databricks"]]
    assert real_calls and all(kw["api_key"] == "only-set-by-this-test" for kw in real_calls)
