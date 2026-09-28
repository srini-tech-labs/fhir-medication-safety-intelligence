"""OpenAI-backed explanation service (Responses API) -- an evaluation candidate, not deployed.

Same prompt, schema, guard and fallbacks as the Claude API provider (``claude.py``); only the transport
differs. The Responses API's ``text.format`` (json_schema, strict mode) accepts ``RESPONSE_SCHEMA``
unmodified: it is already strict-mode-compliant (all fields required, ``additionalProperties: false``
throughout), so no schema relaxation or post-response re-validation is needed on the success path --
this mirrors Claude's own ``text_format`` path, not Databricks' (or Amazon Nova's) tool-relaxation path.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from app.redact import harden_sdk_logging
from app.services.explanation.claude import RESPONSE_SCHEMA, ClaudeExplanationService, Completion
from app.terminology import Terminology

SCHEMA_NAME = "medication_safety_explanation"


class OpenAIExplanationService(ClaudeExplanationService):
    def __init__(self, package_dir: Path, terminology: Terminology, model: str, *, timeout_seconds: float = 20.0,
                 client=None, max_tokens: int = 4000):
        super().__init__(package_dir, terminology, model, client=client, max_tokens=max_tokens)
        self._timeout = timeout_seconds

    def _get_client(self):
        if self._client is None:
            import openai  # optional dependency ("eval" extra)

            harden_sdk_logging()  # importing the SDK may re-apply its own debug logging; its DEBUG logs dump request bodies
            # Explicit, not `openai.OpenAI(timeout=...)` with no api_key: read OPENAI_API_KEY out and pass it
            # in, the same way GeminiExplanationService/DatabricksExplanationService already read their own
            # credential env vars explicitly, rather than relying on the SDK's own implicit os.environ lookup.
            self._client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"), timeout=self._timeout)
        return self._client

    def _complete(self, model_input: dict) -> Completion:
        response = self._get_client().responses.create(
            model=self._model,
            instructions=self._system,
            input=json.dumps(model_input, indent=2),
            max_output_tokens=self._max_tokens,
            text={"format": {"type": "json_schema", "name": SCHEMA_NAME, "schema": RESPONSE_SCHEMA, "strict": True}},
        )
        return self._from_response(response)

    def _from_response(self, response) -> Completion:
        """Normalise a Responses API result into the Claude stop-reason vocabulary. No custom error
        wrapping is needed around the call itself: the ``openai`` SDK's own exceptions already carry a
        ``.status_code`` int and transient-class names (``APIConnectionError``/``APITimeoutError``) that
        ``failures.classify()`` already understands."""
        model = getattr(response, "model", None) or self._model
        status = getattr(response, "status", None) or "completed"
        text, refusal = "", False
        for item in getattr(response, "output", None) or []:
            for part in getattr(item, "content", None) or []:
                ptype = getattr(part, "type", None)
                if ptype == "refusal":
                    refusal = True
                elif ptype == "output_text":
                    text = getattr(part, "text", None) or text
        if refusal:
            return Completion(text=text, stop_reason="refusal", model=model)
        if status == "completed":
            return Completion(text=text, stop_reason="end_turn", model=model)
        if status == "incomplete":
            reason = getattr(getattr(response, "incomplete_details", None), "reason", None)
            stop = "max_tokens" if reason == "max_output_tokens" else f"incomplete:{reason or 'unknown'}"
            return Completion(text=text, stop_reason=stop, model=model)
        return Completion(text=text, stop_reason=f"status:{status}", model=model)
