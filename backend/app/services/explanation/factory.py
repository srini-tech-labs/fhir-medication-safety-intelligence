"""Explanation service selection: real model when credentials are present, otherwise the mock."""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Callable

from app.config import VALID_EXPLANATION_PROVIDERS, Settings
from app.models.contract import AIExplanation, Analysis, Snapshot
from app.redact import redact
from app.repository.base import ClinicalRepository
from app.services.explanation.base import ExplanationService
from app.services.explanation.failures import classify
from app.services.explanation.mock import MockExplanationService
from app.terminology import Terminology

log = logging.getLogger(__name__)

_CREDENTIAL_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")

# `auto` mode: providers with an implicit credential (an IAM role) are always attempted; providers with an
# explicit API key are only attempted once that key is present, exactly like Anthropic's own _CREDENTIAL_ENV
# check above. Databricks needs BOTH host and token (either alone is useless); the others need just one.
_ROLE_BASED_PROVIDERS = {"bedrock"}
_CREDENTIAL_ENV_BY_PROVIDER = {
    "anthropic": _CREDENTIAL_ENV,
    "openai": ("OPENAI_API_KEY",),
    "gemini": ("GEMINI_API_KEY",),
    "databricks": ("DATABRICKS_HOST", "DATABRICKS_TOKEN"),
}


def _has_credentials(provider: str) -> bool:
    names = _CREDENTIAL_ENV_BY_PROVIDER[provider]
    if provider == "databricks":
        return all(os.getenv(v) for v in names)
    return any(os.getenv(v) for v in names)


class SafeExplanationService(ExplanationService):
    """Runs the primary (LLM) service; on ANY failure returns the labelled deterministic mock.

    The explanation layer is optional: an API error, refusal, or grounding violation must never
    break an analysis. If ``on_rejection`` is given, a failure that concerns a model completion is also captured there
    (exact raw output + category) for review; capturing can never break the fallback.
    """

    def __init__(self, primary: ExplanationService, fallback: MockExplanationService,
                 on_rejection: Callable[[dict], None] | None = None):
        self._primary = primary
        self._fallback = fallback
        self._on_rejection = on_rejection

    def explain(self, patient_snapshot: Snapshot, deterministic_analysis: Analysis,
                note_context: str | None = None) -> AIExplanation:
        try:
            return self._primary.explain(patient_snapshot, deterministic_analysis, note_context)
        except Exception as exc:  # noqa: BLE001
            code = classify(exc)  # only this public code/message ever leaves the server
            log.warning("AI explanation fell back to the deterministic mock: code=%s cause=%s",
                        code, redact(f"{type(exc).__name__}: {exc}")[:300])  # raw cause: redacted server log only
            self._capture(exc, code, deterministic_analysis)
            return self._fallback.explain(patient_snapshot, deterministic_analysis, note_context,
                                          fallback_code=code)

    def _capture(self, exc: Exception, code: str, analysis: Analysis) -> None:
        raw = getattr(exc, "raw_output", None)
        if self._on_rejection is None or raw is None:  # provider errors have no model output to review
            return
        try:
            self._on_rejection({
                "capturedAt": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
                "analysisId": analysis.analysis_id,
                "patientId": analysis.patient_id,
                "model": getattr(exc, "model_name", None),
                "code": code,
                "category": redact(str(exc))[:500],
                "flagged": [{"category": f["category"], "sentence": redact(f["sentence"])}
                            for f in getattr(exc, "flagged", [])],
                "rawOutputChars": len(raw),
                "rawOutput": redact(raw),  # exactly what the model returned (credential-shaped strings redacted)
            })
        except Exception as capture_error:  # noqa: BLE001 - diagnostics must never affect the fallback
            log.warning("could not capture rejected explanation: %s", type(capture_error).__name__)


def create_explanation_service(settings: Settings, repository: ClinicalRepository | None = None) -> ExplanationService:
    """EXPLANATION_MODE: ``mock`` = never call a model; ``auto`` (default) = use the selected EXPLANATION_PROVIDER
    only once its own credential(s) are present (role-based providers like Bedrock are always attempted);
    ``claude`` = always try the selected provider, falling back to the mock on failure."""
    mock = MockExplanationService()
    mode = settings.explanation_mode
    provider = settings.explanation_provider
    if provider not in VALID_EXPLANATION_PROVIDERS:
        raise ValueError(f"Unsupported EXPLANATION_PROVIDER: {provider!r} (valid: {', '.join(VALID_EXPLANATION_PROVIDERS)})")
    if mode == "mock":
        return mock
    # In Lambda, OPENAI_API_KEY is never set directly (never packaged, never in .local/providers.env): fetch it
    # from Secrets Manager into that same env var, once, BEFORE the credential-gate check below -- otherwise a
    # correctly-configured secret would still look like "no credential" to _has_credentials() and silently fall
    # back to mock. A no-op everywhere else (local dev, the eval harness): OPENAI_API_KEY is already set there,
    # or openai_api_key_secret_arn is unset, so load_secret_env() returns immediately either way.
    if provider == "openai" and settings.openai_api_key_secret_arn:
        from app.secrets import load_secret_env

        try:
            load_secret_env("OPENAI_API_KEY", settings.openai_api_key_secret_arn)
        except Exception as exc:  # noqa: BLE001 - Secrets Manager unavailable/denied must degrade, never crash the app
            log.warning("Could not load OPENAI_API_KEY from Secrets Manager; using mock (%s)",
                        redact(f"{type(exc).__name__}: {exc}"))
            return mock
    # `auto` waits for an explicit provider's own API key; a role-based provider (e.g. Bedrock, IAM role) means "use it".
    if mode == "auto" and provider not in _ROLE_BASED_PROVIDERS and not _has_credentials(provider):
        return mock
    try:
        if provider == "bedrock":
            from app.services.explanation.bedrock import BedrockClaudeExplanationService

            claude = BedrockClaudeExplanationService(
                settings.package_dir, Terminology.load(settings.package_dir), settings.explanation_model,
                region=settings.bedrock_region, timeout_seconds=settings.bedrock_timeout_seconds, max_tokens=settings.bedrock_max_tokens,
                structured_output=settings.bedrock_structured_output)
        elif provider == "openai":
            from app.services.explanation.openai import OpenAIExplanationService

            claude = OpenAIExplanationService(
                settings.package_dir, Terminology.load(settings.package_dir), settings.explanation_model,
                timeout_seconds=settings.openai_timeout_seconds, max_tokens=settings.openai_max_tokens)
        elif provider == "gemini":
            from app.services.explanation.gemini import GeminiExplanationService

            claude = GeminiExplanationService(
                settings.package_dir, Terminology.load(settings.package_dir), settings.explanation_model,
                timeout_seconds=settings.gemini_timeout_seconds, max_tokens=settings.gemini_max_tokens)
        elif provider == "databricks":
            from app.services.explanation.databricks import DatabricksExplanationService

            claude = DatabricksExplanationService(
                settings.package_dir, Terminology.load(settings.package_dir), settings.databricks_endpoint,
                host=settings.databricks_host, timeout_seconds=settings.databricks_timeout_seconds,
                max_tokens=settings.databricks_max_tokens, structured_output=settings.databricks_structured_output)
        else:
            from app.services.explanation.claude import ClaudeExplanationService

            claude = ClaudeExplanationService(
                settings.package_dir, Terminology.load(settings.package_dir), settings.explanation_model)
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not initialise the Claude explanation service; using mock (%s)",
                    redact(f"{type(exc).__name__}: {exc}"))
        return mock
    sink = repository.save_rejected_explanation if repository and settings.capture_rejected_explanations else None
    return SafeExplanationService(claude, mock, on_rejection=sink)
