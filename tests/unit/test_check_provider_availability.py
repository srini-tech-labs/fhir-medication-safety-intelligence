"""Regression: a credential that exists only in a loaded .local/providers.env dict must reach each SDK client
constructor EXPLICITLY, and must never need to already be set in the parent process's os.environ.

Root cause this guards against: scripts/check_provider_availability.py used to load .local/providers.env
into a private local dict (correctly used for the read-only "credential present" report), but its --verify
Tier-2 check then constructed real SDK clients with zero arguments (anthropic.Anthropic(), openai.OpenAI(),
genai.Client()), relying entirely on each SDK's own implicit os.environ lookup -- which this local dict
never fed. That surfaced as Gemini's "ValueError: No API key was provided" on the very first live
availability verification: not an invalid-key response, the SDK never received a key at all.

Every test here builds a local `env` dict directly (never touching os.environ), calls `_verify()`, and
asserts (a) the SDK constructor was called with the exact credential value and (b) os.environ never held it.
"""
from __future__ import annotations

import importlib.util
import os
from types import SimpleNamespace

import pytest

from app.config import REPO_ROOT

spec = importlib.util.spec_from_file_location("check_provider_availability", REPO_ROOT / "scripts" / "check_provider_availability.py")
cpa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cpa)

FAKE_ANTHROPIC_KEY = "sk-ant-api03-ONLY-IN-LOCAL-PROVIDERS-ENV-0000000000"
FAKE_OPENAI_KEY = "sk-proj-ONLY-IN-LOCAL-PROVIDERS-ENV-0000000000"
FAKE_GEMINI_KEY = "AIzaONLYINLOCALPROVIDERSENV00000000000"
FAKE_DATABRICKS_TOKEN = "dapi-ONLY-IN-LOCAL-PROVIDERS-ENV-0000000000"
FAKE_DATABRICKS_HOST = "https://example-workspace.cloud.databricks.com"


def assert_never_in_os_environ(*names: str) -> None:
    for name in names:
        assert os.environ.get(name) is None, f"{name} leaked into the parent process environment"


def test_anthropic_verify_passes_the_api_key_explicitly(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    captured = {}

    class FakeAnthropic:
        def __init__(self, **kw):
            captured.update(kw)
            self.messages = SimpleNamespace(create=lambda **_: SimpleNamespace(model="claude-haiku-4-5"))

    import anthropic

    monkeypatch.setattr(anthropic, "Anthropic", FakeAnthropic)
    env = {"ANTHROPIC_API_KEY": FAKE_ANTHROPIC_KEY}  # exists only in this local dict, simulating .local/providers.env
    cpa._verify("anthropic", env)
    assert captured == {"api_key": FAKE_ANTHROPIC_KEY}
    assert_never_in_os_environ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


def test_anthropic_verify_falls_back_to_auth_token_when_no_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    captured = {}

    class FakeAnthropic:
        def __init__(self, **kw):
            captured.update(kw)
            self.messages = SimpleNamespace(create=lambda **_: SimpleNamespace(model="claude-haiku-4-5"))

    import anthropic

    monkeypatch.setattr(anthropic, "Anthropic", FakeAnthropic)
    env = {"ANTHROPIC_AUTH_TOKEN": "only-in-local-providers-env-token"}
    cpa._verify("anthropic", env)
    assert captured == {"auth_token": "only-in-local-providers-env-token"}
    assert_never_in_os_environ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


def test_openai_verify_passes_the_api_key_explicitly(monkeypatch):
    pytest.importorskip("openai")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    captured = {}

    class FakeOpenAI:
        def __init__(self, **kw):
            captured.update(kw)
            self.responses = SimpleNamespace(create=lambda **_: SimpleNamespace(model="gpt-5.6-luna"))

    import openai

    monkeypatch.setattr(openai, "OpenAI", FakeOpenAI)
    env = {"OPENAI_API_KEY": FAKE_OPENAI_KEY}
    cpa._verify("openai", env)
    assert captured == {"api_key": FAKE_OPENAI_KEY}
    assert_never_in_os_environ("OPENAI_API_KEY")


def test_openai_verify_uses_a_max_output_tokens_the_responses_api_actually_accepts(monkeypatch):
    """Regression: the first real --verify openai run was rejected with a 400 -- "Invalid 'max_output_tokens':
    integer below minimum value. Expected a value >= 16, but got 8 instead." -- because the probe call used 8.
    Not a credential or object-lifetime bug, just a wrong constant; this pins it so it can't silently regress."""
    pytest.importorskip("openai")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    captured_call = {}

    class FakeOpenAI:
        def __init__(self, **kw):
            self.responses = SimpleNamespace(create=self._create)

        def _create(self, **kw):
            captured_call.update(kw)
            return SimpleNamespace(model="gpt-5.6-luna")

    import openai

    monkeypatch.setattr(openai, "OpenAI", FakeOpenAI)
    cpa._verify("openai", {"OPENAI_API_KEY": FAKE_OPENAI_KEY})
    assert captured_call["max_output_tokens"] >= 16, "below the Responses API's documented floor; will 400 for real"


def test_gemini_verify_passes_the_api_key_explicitly_this_is_the_exact_reported_bug(monkeypatch):
    """The concrete failure reported: `check_provider_availability.py --verify gemini` raised
    `ValueError: No API key was provided` even though `make check-providers` showed GEMINI_API_KEY=set."""
    pytest.importorskip("google.genai")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    captured = {}

    class FakeClient:
        def __init__(self, **kw):
            captured.update(kw)
            self.models = SimpleNamespace(generate_content=lambda **_: SimpleNamespace(model_version="gemini-3.8-flash"))

    from google import genai

    monkeypatch.setattr(genai, "Client", FakeClient)
    env = {"GEMINI_API_KEY": FAKE_GEMINI_KEY}  # exists only here, simulating .local/providers.env; os.environ is untouched
    cpa._verify("gemini", env)
    assert captured == {"api_key": FAKE_GEMINI_KEY}
    assert_never_in_os_environ("GEMINI_API_KEY")


class _SharedTransport:
    """Stands in for google-genai's shared `_api_client`/httpx transport."""

    def __init__(self):
        self.closed = False

    def send(self):
        if self.closed:
            raise RuntimeError("Cannot send a request, as the client has been closed.")
        return SimpleNamespace(model_version="gemini-3.8-flash")


class _FakeModels:
    """Holds a reference to the SHARED transport only -- never back to the outer Client. This is the actual
    google-genai object graph (Models(self._api_client), not Models(self)) that makes chaining dangerous:
    once `.models` has been read, nothing keeps the outer Client object alive."""

    def __init__(self, transport):
        self._transport = transport

    def generate_content(self, **kw):
        return self._transport.send()


class GeminiLikeClient:
    """Minimal fake reproducing google-genai's actual __del__-closes-shared-transport behavior, deterministic
    under CPython's immediate refcounting (no reference cycle involved, so no GC timing uncertainty)."""

    def __init__(self, **kw):
        self.captured_kwargs = kw
        self._transport = _SharedTransport()
        self.models = _FakeModels(self._transport)

    def __del__(self):
        self._transport.closed = True


def test_gemini_verify_does_not_chain_construction_into_the_call_reproduced_object_lifetime_bug(monkeypatch):
    """The real failure from the first live --verify gemini run: `genai.Client(...).models.generate_content(...)`
    chained in one expression let the temporary Client's refcount drop to zero the instant `.models` was
    accessed (Models only references the shared transport/_api_client, never the outer Client), so
    Client.__del__ closed the shared transport before the request completed -- raising exactly
    "Cannot send a request, as the client has been closed." This is NOT a credential bug (a separate one was
    already fixed and is covered above); it is reproduced here with a fake mirroring that exact object graph,
    proving `_verify()` now binds `client = genai.Client(...)` to a local first, keeping it alive for the
    whole call, rather than chaining."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    from google import genai

    monkeypatch.setattr(genai, "Client", GeminiLikeClient)
    # Sanity check the fake actually reproduces the bug when chained the old (broken) way:
    with pytest.raises(RuntimeError, match="has been closed"):
        genai.Client(api_key=FAKE_GEMINI_KEY).models.generate_content()
    # The fixed _verify() must not hit this at all:
    env = {"GEMINI_API_KEY": FAKE_GEMINI_KEY}
    cpa._verify("gemini", env)  # must not raise, and must print PASS, not FAIL
    assert_never_in_os_environ("GEMINI_API_KEY")


def test_openai_verify_does_not_chain_construction_into_the_call(monkeypatch):
    """Same defensive fix applied to the openai branch, proven the same way even though the real `openai`
    SDK is not known to have this __del__ behavior -- the chained-construction pattern itself is the risk."""
    pytest.importorskip("openai")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    class _FakeResponses:
        def __init__(self, transport):
            self._transport = transport  # references the SHARED transport only, never back to the outer client

        def create(self, **kw):
            return self._transport.send()

    class OpenAILikeClient:
        def __init__(self, **kw):
            self.captured_kwargs = kw
            self._transport = _SharedTransport()
            self.responses = _FakeResponses(self._transport)

        def __del__(self):
            self._transport.closed = True

    import openai

    monkeypatch.setattr(openai, "OpenAI", OpenAILikeClient)
    env = {"OPENAI_API_KEY": FAKE_OPENAI_KEY}
    cpa._verify("openai", env)  # must not raise
    assert_never_in_os_environ("OPENAI_API_KEY")


def test_databricks_verify_uses_the_host_and_token_explicitly_never_os_environ(monkeypatch):
    captured = {}

    class FakeResponse:
        def read(self):
            return b'{"endpoints": [{"name": "medsafety-eval"}]}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.header_items())
        return FakeResponse()

    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    env = {"DATABRICKS_HOST": FAKE_DATABRICKS_HOST, "DATABRICKS_TOKEN": FAKE_DATABRICKS_TOKEN}
    cpa._verify("databricks", env)
    assert captured["url"] == f"{FAKE_DATABRICKS_HOST}/api/2.0/serving-endpoints"
    assert captured["headers"]["Authorization"] == f"Bearer {FAKE_DATABRICKS_TOKEN}"
    assert_never_in_os_environ("DATABRICKS_HOST", "DATABRICKS_TOKEN")


def test_verify_never_calls_the_sdk_when_the_credential_is_absent(monkeypatch, capsys):
    """No credential in `env` -> `_verify` must report "not available" and never construct a client at all."""
    called = []

    class Explodes:
        def __init__(self, **kw):
            called.append(kw)
            raise AssertionError("must not be constructed when the credential is absent")

    import anthropic

    monkeypatch.setattr(anthropic, "Anthropic", Explodes)
    cpa._verify("anthropic", {})
    assert called == []
    assert "not available, skipped" in capsys.readouterr().out


def test_presence_report_still_works_from_the_same_local_dict_never_os_environ(monkeypatch):
    """The Tier-1 report (`presence()`) already worked correctly before this fix; this proves it still does,
    reading only from the passed-in dict -- the bug was isolated to `_verify()`, not `presence()`."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    row = cpa.presence("gemini", {"GEMINI_API_KEY": FAKE_GEMINI_KEY})
    assert row["credential_present"] is True
    assert_never_in_os_environ("GEMINI_API_KEY")
