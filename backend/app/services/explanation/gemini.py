"""Google Gemini explanation service (``google-genai`` SDK) -- an evaluation candidate, not deployed.

Same prompt, schema, guard and fallbacks as the Claude API provider (``claude.py``); only the transport
differs.

APPROVED SCHEMA APPROACH (final): ``RESPONSE_SCHEMA`` is passed to Gemini verbatim via
``response_json_schema``. There is NO Gemini-specific schema weakening, derivation, or transformation of
any kind anywhere in this file. This differs from Amazon Nova and Databricks, which do need a relaxed
wire schema; Gemini does not, because the current ``google-genai`` SDK's ``response_json_schema`` field
(distinct from the older, restricted ``response_schema``/OpenAPI-dialect field) accepts standard JSON
Schema directly -- ``anyOf``, ``additionalProperties``, ``required``, null types included.

Post-response validation against the ORIGINAL ``RESPONSE_SCHEMA`` (``schema_check``) still runs
unconditionally before the grounding guard, the same defense-in-depth every provider applies, regardless
of whether the wire schema was trusted to be exact.

Fail loud, never silently degrade: if the installed SDK or a live call rejects any part of the schema, that
surfaces as a specific, reported error (the redacted exception text names the rejected feature) -- it is
never caught and silently retried with an altered schema.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from app.redact import harden_sdk_logging, redact
from app.services.explanation import failures
from app.services.explanation.base import ExplanationUnavailable
from app.services.explanation.claude import RESPONSE_SCHEMA, ClaudeExplanationService, Completion
from app.services.explanation.schema_check import validate as validate_schema
from app.terminology import Terminology

_STOP_REASONS = {"SAFETY": "refusal", "RECITATION": "refusal", "MAX_TOKENS": "max_tokens"}
_TRANSIENT_NAMES = {"ServerError", "DeadlineExceededError", "ResourceExhaustedError"}


class GeminiExplanationService(ClaudeExplanationService):
    def __init__(self, package_dir: Path, terminology: Terminology, model: str, *, timeout_seconds: float = 20.0,
                 base_url: str | None = None, client=None, max_tokens: int = 4000):
        super().__init__(package_dir, terminology, model, client=client, max_tokens=max_tokens)
        self._timeout = timeout_seconds
        # Override for tests/an on-prem gateway only; unset uses the SDK's real endpoint. The constructor arg wins;
        # GEMINI_BASE_URL is a plain env-var fallback, the same convention OPENAI_BASE_URL/ANTHROPIC_BASE_URL already
        # use for their SDKs (read directly, no app wiring needed) -- this is how the SDK-logging probe redirects
        # the real SDK at a local fake server without touching Settings/factory.
        self._base_url = base_url or os.getenv("GEMINI_BASE_URL")

    def _get_client(self):
        if self._client is None:
            from google import genai  # optional dependency ("eval" extra)

            harden_sdk_logging()
            # http_options.timeout is milliseconds (google-genai convention, distinct from most other SDKs' seconds)
            http_options = genai.types.HttpOptions(timeout=int(self._timeout * 1000), base_url=self._base_url)
            self._client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"), http_options=http_options)
        return self._client

    def _generate(self, model_input: dict):
        # A plain dict, not `google.genai.types.GenerateContentConfig`: the SDK accepts either (its methods are typed
        # `...ConfigOrDict`), and using a dict here means constructing a request never needs the SDK installed --
        # only `_get_client()` does (see `_get_client` above), matching every other provider's lazy-import boundary.
        config = {
            "system_instruction": self._system,
            "response_mime_type": "application/json",
            "response_json_schema": RESPONSE_SCHEMA,  # unmodified -- see module docstring
            "max_output_tokens": self._max_tokens,
        }
        try:
            return self._get_client().models.generate_content(
                model=self._model, contents=json.dumps(model_input, indent=2), config=config)
        except Exception as exc:  # noqa: BLE001 - google-genai transport/API errors, including a rejected schema
            name = type(exc).__name__
            status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
            transient = name in _TRANSIENT_NAMES or (isinstance(status, int) and (status in (408, 409, 429) or status >= 500))
            # the exception text may name the exact rejected schema feature; kept (redacted) so it is reported, not hidden
            detail = redact(str(exc))[:240]
            raise ExplanationUnavailable(f"gemini generate_content failed: {name}: {detail}",
                                         failures.TEMPORARY if transient else failures.UNAVAILABLE) from exc

    def _complete(self, model_input: dict) -> Completion:
        response = self._generate(model_input)
        candidates = getattr(response, "candidates", None) or []
        candidate = candidates[0] if candidates else None
        finish = getattr(candidate, "finish_reason", None)
        finish_name = getattr(finish, "name", finish) or "STOP"
        model = getattr(response, "model_version", None) or self._model
        text = getattr(response, "text", None) or ""

        if finish_name in _STOP_REASONS:
            return Completion(text=text, stop_reason=_STOP_REASONS[finish_name], model=model)
        if finish_name != "STOP":
            return Completion(text=text, stop_reason=f"finish:{finish_name}", model=model)

        # Post-response validation, mandatory regardless of the trusted wire schema (see module docstring).
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            unavailable = ExplanationUnavailable("gemini output was not valid JSON", failures.INCOMPLETE)
            unavailable.raw_output, unavailable.model_name = text, model
            raise unavailable from exc
        problems = validate_schema(payload, RESPONSE_SCHEMA)
        if problems:
            exc = ExplanationUnavailable("gemini output violates the response schema: " + "; ".join(problems[:3]),
                                         failures.INCOMPLETE)
            exc.raw_output, exc.model_name = text, model
            raise exc
        return Completion(text=text, stop_reason="end_turn", model=model)
