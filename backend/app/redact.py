"""Log safety: secret redaction, plus keeping HTTP/SDK debug output (which carries request bodies) off.

Defense in depth: application code never logs prompts, patient data or credentials, but two things
outside our control can: exception text from HTTP/SDK layers (surfaced to users as ``fallbackReason``)
and the Anthropic SDK's own DEBUG logging, which dumps the full request body (``"Request options: ..."``
in ``anthropic._base_client``) -- i.e. the patient snapshot and note text. The SDK offers no redaction or
switch for this, so production safety must not depend on DEBUG staying disabled.
"""
from __future__ import annotations

import logging
import os
import re

REDACTED = "[REDACTED]"
_CREDENTIAL_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "GEMINI_API_KEY", "DATABRICKS_TOKEN")
# DATABRICKS_HOST is a hostname, not a secret -- deliberately excluded, same as BEDROCK_REGION.

# Loggers whose DEBUG/INFO output can contain request/response content: the Anthropic/OpenAI/Google SDKs and their HTTP
# stack, and the AWS stack (httpx logs full request URLs incl. query values at INFO; botocore logs canonical requests,
# headers and SigV4 signatures at DEBUG; boto3/urllib3 likewise). Databricks reuses the `openai` SDK/HTTP stack (see
# databricks.py), so no separate Databricks logger name is needed here.
_SDK_LOGGERS = ("anthropic", "anthropic._base_client", "anthropic._response",
                "openai", "google_genai", "google.genai",
                "httpx", "httpx2", "httpcore", "httpcore2",  # httpx2/httpcore2 are what this SDK version uses
                "botocore", "boto3", "urllib3", "s3transfer")
_SDK_PREFIXES = ("anthropic", "openai", "google_genai", "google.genai",
                 "httpx", "httpx2", "httpcore", "httpcore2", "botocore", "boto3", "urllib3", "s3transfer")
SUPPRESSED = "[SDK/HTTP debug output suppressed]"

_PATTERNS = (
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{6,}"),  # Anthropic API keys / admin keys
    re.compile(r"sk-(?:proj-|svcacct-)?[A-Za-z0-9_\-]{16,}"),  # OpenAI API keys (sk-ant- is already redacted by the pattern above by the time this runs)
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),  # Google API keys (Gemini); real keys are 39 chars total, but an
    # open-ended minimum (matching the sk-ant-/sk- patterns' style above) avoids a silent miss if a differently
    # sized key/token ever appears -- a fixed {35} previously let a too-short fake key slip through (caught by
    # scripts/live_check.py --provider gemini --selftest, which found the gap this fixes)
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/\-]{8,}=*"),  # Authorization: Bearer <token> (also covers Databricks tokens)
    re.compile(r"(?i)x-api-key[\"']?\s*[:=]\s*[\"']?[^\s\"',}]+"),  # x-api-key: <value>
    re.compile(r"dapi[0-9a-fA-F]{20,}"),  # Databricks personal access tokens; a pattern-based fallback for when the
    # exact DATABRICKS_TOKEN env value isn't set/matching at redact() time -- unlike the other three providers,
    # this codebase previously had no shape-based fallback for Databricks tokens at all (found while writing
    # scripts/diagnose_databricks_400.py's tests, which construct sanitized error text independent of any real
    # env var and caught the gap).
)


def redact(text: str) -> str:
    """Remove the configured credential values and anything shaped like an API key/bearer token."""
    for var in _CREDENTIAL_ENV:
        value = os.getenv(var)
        if value and len(value) >= 8:  # ignore trivially short values that would mangle normal text
            text = text.replace(value, REDACTED)
    for pattern in _PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


def harden_sdk_logging() -> None:
    """Pin the SDK/HTTP loggers to WARNING so their DEBUG code paths (which serialise request bodies) never run.

    Uses plain stdlib logging. Explicit levels on the *child* loggers survive ``ANTHROPIC_LOG=debug`` (the SDK
    only sets the parent ``anthropic``/``httpx2`` loggers, at import time), and a root logger at DEBUG does
    not override an explicit level. Call again after importing the SDK, because the SDK re-applies
    ``ANTHROPIC_LOG`` on import.
    """
    for name in _SDK_LOGGERS:
        logger = logging.getLogger(name)
        if logger.level < logging.WARNING:  # NOTSET or DEBUG/INFO -> pin; never lowers a stricter level
            logger.setLevel(logging.WARNING)


def _is_sdk_logger(name: str) -> bool:
    return any(name == p or name.startswith(p + ".") for p in _SDK_PREFIXES)


def install_log_redaction() -> None:
    """Harden SDK logging and redact every log record as it is created. Idempotent.

    - Pins the SDK/HTTP loggers (``harden_sdk_logging``).
    - Backstop: any DEBUG *or INFO* record that still gets created by an SDK/HTTP logger (e.g. someone
      re-enabled it -- the exact scenario the multi-provider evaluation harness's own "logging hygiene"
      check deliberately forces, to prove this holds even then) has its message, arguments and traceback
      replaced, so request bodies can never be emitted. INFO is included, not just DEBUG: httpx logs the
      full request URL at INFO by default (documented above), and for Databricks that URL contains the
      workspace host -- confirmed to reach real log output this way on the first live Databricks run,
      even though the bearer token itself never did (see tests/unit/test_databricks_logging.py). Only
      WARNING and above pass through from an SDK/HTTP logger (e.g. "Retrying request..."), still redacted.
    - Redacts credentials from the formatted message of every other record. Traceback text is formatted
      later, so call sites that may see credential-bearing exceptions log ``redact(str(exc))`` instead of
      using ``log.exception``.
    """
    harden_sdk_logging()
    previous = logging.getLogRecordFactory()
    if getattr(previous, "_redacting", False):
        return

    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        if record.levelno <= logging.INFO and _is_sdk_logger(record.name):
            record.msg, record.args, record.exc_info, record.exc_text = SUPPRESSED, (), None, None
            return record
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - never let logging break the app
            return record
        cleaned = redact(message)
        if cleaned != message:
            record.msg, record.args = cleaned, ()
        return record

    factory._redacting = True  # type: ignore[attr-defined]
    logging.setLogRecordFactory(factory)
