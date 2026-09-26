"""Zero-leakage proof for the Gemini provider (google-genai SDK).

Mirrors test_sdk_logging.py's structure and guarantee exactly: with everything at DEBUG, no patient/note
content or the Google-key-shaped credential may reach any log record, and a "baseline" (unhardened) run
first proves the probe actually observes a real call, so the "hardened" pass is not vacuous.

Skipped entirely when `google-genai` (the "eval" extra) is not installed -- `make test` stays green
without it.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys

import pytest

pytest.importorskip("google.genai")

from app.config import REPO_ROOT
from app.redact import SUPPRESSED, _SDK_LOGGERS, harden_sdk_logging, install_log_redaction

PROBE = REPO_ROOT / "tests" / "support" / "sdk_logging_probe_gemini.py"
KEY = "AIzaSyLOGPROBE00000000000000000000000"  # Google API-key shape, fake
CONTENT = ["Lisa Demo", "Recent discharge summary", "Potassium was elevated", "structuredData", "deterministicFindings"]


def run_probe(mode: str) -> dict:
    env = {**os.environ, "GEMINI_API_KEY": KEY, "NO_PROXY": "127.0.0.1"}
    proc = subprocess.run([sys.executable, str(PROBE), mode], env=env, capture_output=True, text=True, timeout=120,
                          cwd=REPO_ROOT / "backend")
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
def test_probe_is_sensitive_the_unhardened_real_sdk_can_leak_request_bodies():
    """Guards against a vacuous pass: without app hardening the probe DOES observe a real HTTP call."""
    baseline = run_probe("baseline")
    assert baseline["mode"] == "llm" and baseline["requests_seen"] == 1


@pytest.mark.parametrize("mode", ["hardened", "client-only"])
def test_app_emits_no_patient_note_or_credential_even_with_everything_at_debug(mode):
    """`hardened` = create_app() ran (uvicorn startup). `client-only` = the client factory alone, e.g. scripts/workers."""
    probe = run_probe(mode)
    assert probe["mode"] == "llm" and probe["requests_seen"] == 1  # the live-client path still works
    for needle in [*CONTENT, KEY, "AIza"]:
        assert hits(probe, needle) == 0, f"{needle!r} reached a log record ({mode})"
    sdk_debug = [r for r in probe["logs"]
                if r[1].split(".")[0] in ("google_genai", "google", "httpx", "httpx2", "httpcore", "httpcore2")
                and r[0] == "DEBUG" and r[2] != SUPPRESSED]
    assert sdk_debug == []  # no unsuppressed SDK/HTTP-stack debug output at all


# ---- in-process unit coverage: the Gemini logger names are pinned the same way "anthropic"/"openai" already are --
@pytest.mark.parametrize("logger_name", ["google_genai", "google.genai"])
def test_harden_pins_the_gemini_logger_so_a_root_debug_cannot_enable_body_dumping(restore_logging_levels, logger_name):
    logging.getLogger(logger_name).setLevel(logging.NOTSET)
    root_level = logging.getLogger().level
    logging.getLogger().setLevel(logging.DEBUG)
    try:
        assert logging.getLogger(logger_name).isEnabledFor(logging.DEBUG)  # premise: exposed
        harden_sdk_logging()
        assert not logging.getLogger(logger_name).isEnabledFor(logging.DEBUG)
    finally:
        logging.getLogger().setLevel(root_level)


def test_backstop_neutralises_gemini_debug_records_even_if_someone_re_enables_debug(caplog, restore_logging_levels):
    install_log_redaction()
    sdk = logging.getLogger("google_genai")
    sdk.setLevel(logging.DEBUG)  # deliberate re-enable, defeating the pin
    with caplog.at_level(logging.DEBUG):
        sdk.debug("request body: %s", {"contents": "Lisa Demo potassium 5.9"})
    assert "Lisa Demo" not in caplog.text and "5.9" not in caplog.text
    assert SUPPRESSED in caplog.text
