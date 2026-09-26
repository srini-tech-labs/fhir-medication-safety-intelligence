"""Anthropic SDK DEBUG logging dumps the full request body (patient snapshot + note text).

Production safety must not depend on DEBUG staying off: with root logging, the app and the SDK all at
DEBUG (and ANTHROPIC_LOG=debug), no patient/note content or credential may reach any log record.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys

import pytest

from app.config import REPO_ROOT
from app.redact import SUPPRESSED, _SDK_LOGGERS, harden_sdk_logging, install_log_redaction

PROBE = REPO_ROOT / "tests" / "support" / "sdk_logging_probe.py"
KEY = "sk-ant-api03-LOGPROBE-000000000000000000000000"
CONTENT = ["Lisa Demo", "Recent discharge summary", "Potassium was elevated", "structuredData", "deterministicFindings"]


def run_probe(mode: str) -> dict:
    env = {**os.environ, "ANTHROPIC_LOG": "debug", "ANTHROPIC_API_KEY": KEY, "NO_PROXY": "127.0.0.1",
           "EXPLANATION_MODE": "claude"}
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
def test_probe_is_sensitive_the_unhardened_real_sdk_does_leak_request_bodies():
    """Guards against a vacuous pass: without app hardening the same probe DOES emit the body."""
    baseline = run_probe("baseline")
    assert baseline["mode"] == "llm" and baseline["requests_seen"] == 1
    for needle in CONTENT:
        assert hits(baseline, needle) >= 1, f"probe failed to observe the known leak of {needle!r}"


@pytest.mark.parametrize("mode", ["hardened", "client-only"])
def test_app_emits_no_patient_note_or_credential_even_with_everything_at_debug(mode):
    """`hardened` = create_app() ran (uvicorn startup). `client-only` = the client factory alone, e.g. scripts/workers."""
    probe = run_probe(mode)
    assert probe["mode"] == "llm" and probe["requests_seen"] == 1  # the live-client path still works
    for needle in [*CONTENT, KEY, "sk-ant-"]:
        assert hits(probe, needle) == 0, f"{needle!r} reached a log record"
    sdk_debug = [r for r in probe["logs"] if r[1].split(".")[0] in ("anthropic", "httpx", "httpx2", "httpcore", "httpcore2")
                 and r[0] == "DEBUG" and r[2] != SUPPRESSED]
    assert sdk_debug == []  # no unsuppressed SDK/HTTP-stack debug output at all


# ---- in-process unit coverage ------------------------------------------------------------------------
def test_harden_pins_sdk_loggers_so_a_root_debug_cannot_enable_body_dumping(restore_logging_levels):
    for name in _SDK_LOGGERS:
        logging.getLogger(name).setLevel(logging.NOTSET)
    root_level = logging.getLogger().level
    logging.getLogger().setLevel(logging.DEBUG)
    try:
        assert logging.getLogger("anthropic._base_client").isEnabledFor(logging.DEBUG)  # premise: exposed
        harden_sdk_logging()
        for name in _SDK_LOGGERS:
            assert not logging.getLogger(name).isEnabledFor(logging.DEBUG), name
        # the SDK only builds the body dump when this is True (see _base_client._build_request)
        assert not logging.getLogger("anthropic._base_client").isEnabledFor(logging.DEBUG)
    finally:
        logging.getLogger().setLevel(root_level)


def test_harden_never_lowers_a_stricter_level(restore_logging_levels):
    logging.getLogger("anthropic").setLevel(logging.ERROR)
    harden_sdk_logging()
    assert logging.getLogger("anthropic").level == logging.ERROR


def test_backstop_neutralises_sdk_debug_records_even_if_someone_re_enables_debug(caplog, restore_logging_levels):
    install_log_redaction()
    sdk = logging.getLogger("anthropic._base_client")
    sdk.setLevel(logging.DEBUG)  # deliberate re-enable, defeating the pin
    with caplog.at_level(logging.DEBUG):
        sdk.debug("Request options: %s", {"json_data": {"messages": [{"content": "Lisa Demo potassium 5.9"}]}})
        sdk.debug("Encountered Exception", exc_info=RuntimeError("body Lisa Demo"))
        logging.getLogger("httpx2").setLevel(logging.DEBUG)
        logging.getLogger("httpx2").debug("body Lisa Demo")
    assert "Lisa Demo" not in caplog.text and "5.9" not in caplog.text
    assert caplog.text.count(SUPPRESSED) == 3
    assert all(r.exc_info is None for r in caplog.records)


def test_backstop_leaves_safe_sdk_records_and_our_own_debug_logs_intact(caplog, restore_logging_levels):
    install_log_redaction()
    with caplog.at_level(logging.DEBUG):
        logging.getLogger("anthropic._base_client").warning("Retrying request to /v1/messages in 0.5 seconds")
        logging.getLogger("app.services").debug("evaluating rules for %s", "P001")
    assert "Retrying request to /v1/messages" in caplog.text
    assert "evaluating rules for P001" in caplog.text
    assert SUPPRESSED not in caplog.text


def test_credentials_are_still_redacted_from_non_sdk_debug_logs(caplog):
    install_log_redaction()
    with caplog.at_level(logging.DEBUG):
        logging.getLogger("app.x").debug("header x-api-key: %s", KEY)
    assert KEY not in caplog.text and "[REDACTED]" in caplog.text
