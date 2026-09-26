"""Zero-leakage proof for the OpenAI and Databricks providers (they share the `openai` SDK/HTTP stack).

Mirrors test_sdk_logging.py's structure and guarantee exactly: with everything at DEBUG, no patient/note
content or credential may reach any log record, and a "baseline" (unhardened) run first proves the probe
actually observes the leak when hardening is absent, so the "hardened" pass is not vacuous.

Skipped entirely when the `openai` package (the "eval" extra) is not installed -- `make test` stays green
without it, exactly like the analogous Bedrock/Nova tests need `boto3` but not `anthropic`, and vice versa.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys

import pytest

pytest.importorskip("openai")

from app.config import REPO_ROOT
from app.redact import SUPPRESSED, _SDK_LOGGERS, harden_sdk_logging, install_log_redaction

PROBE = REPO_ROOT / "tests" / "support" / "sdk_logging_probe_openai.py"
OPENAI_KEY = "sk-proj-LOGPROBE000000000000000000000000"
DATABRICKS_TOKEN = "dapi-LOGPROBE0000000000000000000000000"
CONTENT = ["Lisa Demo", "Recent discharge summary", "Potassium was elevated", "structuredData", "deterministicFindings"]


def run_probe(mode: str, provider: str) -> dict:
    env = {**os.environ, "NO_PROXY": "127.0.0.1"}
    if provider == "openai":
        env["OPENAI_API_KEY"] = OPENAI_KEY
    else:
        env["DATABRICKS_TOKEN"] = DATABRICKS_TOKEN
    proc = subprocess.run([sys.executable, str(PROBE), mode, provider], env=env, capture_output=True, text=True,
                          timeout=120, cwd=REPO_ROOT / "backend")
    assert proc.returncode == 0, proc.stderr[-2000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def hits(probe: dict, needle: str) -> int:
    return sum(needle in message for _, _, message in probe["logs"])


@pytest.fixture()
def restore_logging_levels():
    saved = {n: logging.getLogger(n).level for n in _SDK_LOGGERS}
    yield
    for name, level in saved.items():
        logging.getLogger(name).setLevel(level)


# ---- real SDK, real import order, everything at DEBUG ---------------------------------------------------
@pytest.mark.parametrize("provider", ["openai", "databricks"])
def test_probe_is_sensitive_the_unhardened_real_sdk_can_leak_request_bodies(provider):
    """Guards against a vacuous pass: without app hardening the probe DOES observe a real HTTP call."""
    baseline = run_probe("baseline", provider)
    assert baseline["mode"] == "llm" and baseline["requests_seen"] == 1


@pytest.mark.parametrize("mode", ["hardened", "client-only"])
@pytest.mark.parametrize("provider", ["openai", "databricks"])
def test_app_emits_no_patient_note_or_credential_even_with_everything_at_debug(mode, provider):
    """`hardened` = create_app() ran (uvicorn startup). `client-only` = the client factory alone, e.g. scripts/workers.

    Regression: the first real `--provider databricks` live run reported 10 "credential hits" in the
    harness's own logging-hygiene scan. Traced (offline, no real credentials) to httpx's own INFO-level
    "HTTP Request: POST <url>" line -- one per call, emitted even though the SDK loggers were forced to
    DEBUG (the harness's deliberate "what if debug logging gets re-enabled" stress test) -- because
    install_log_redaction() previously only fully suppressed SDK-logger records BELOW INFO, leaving INFO
    itself exposed. The bearer token was never observed in any record at any level in that same
    reproduction. `"127.0.0.1:"` below stands in for that URL/host leak (the fake server's own address,
    which is exactly the kind of workspace-identifying substring the real DATABRICKS_HOST was)."""
    probe = run_probe(mode, provider)
    assert probe["mode"] == "llm" and probe["requests_seen"] == 1  # the live-client path still works
    key = OPENAI_KEY if provider == "openai" else DATABRICKS_TOKEN
    for needle in [*CONTENT, key, "sk-proj-", "dapi-", "127.0.0.1:", "Authorization", "Bearer"]:
        assert hits(probe, needle) == 0, f"{needle!r} reached a log record ({provider}/{mode})"
    sdk_output = [r for r in probe["logs"] if r[1].split(".")[0] in ("openai", "httpx", "httpx2", "httpcore", "httpcore2")
                 and r[0] in ("DEBUG", "INFO") and r[2] != SUPPRESSED]
    assert sdk_output == []  # no unsuppressed SDK/HTTP-stack DEBUG *or INFO* output at all


# ---- in-process unit coverage: the "openai" logger name is pinned the same way "anthropic" already is ------------
def test_harden_pins_the_openai_logger_so_a_root_debug_cannot_enable_body_dumping(restore_logging_levels):
    logging.getLogger("openai").setLevel(logging.NOTSET)
    root_level = logging.getLogger().level
    logging.getLogger().setLevel(logging.DEBUG)
    try:
        assert logging.getLogger("openai").isEnabledFor(logging.DEBUG)  # premise: exposed
        harden_sdk_logging()
        assert not logging.getLogger("openai").isEnabledFor(logging.DEBUG)
    finally:
        logging.getLogger().setLevel(root_level)


def test_backstop_neutralises_openai_debug_records_even_if_someone_re_enables_debug(caplog, restore_logging_levels):
    install_log_redaction()
    sdk = logging.getLogger("openai")
    sdk.setLevel(logging.DEBUG)  # deliberate re-enable, defeating the pin
    with caplog.at_level(logging.DEBUG):
        sdk.debug("request body: %s", {"input": "Lisa Demo potassium 5.9"})
    assert "Lisa Demo" not in caplog.text and "5.9" not in caplog.text
    assert SUPPRESSED in caplog.text
