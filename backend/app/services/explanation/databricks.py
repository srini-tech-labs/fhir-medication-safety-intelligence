"""Databricks Foundation Model API explanation service -- an evaluation candidate, not deployed.

The Foundation Model API's Chat Completions surface is OpenAI-client-compatible, so this reuses the
``openai`` SDK pointed at the workspace's ``/serving-endpoints`` base URL -- no separate ``databricks-sdk``
runtime dependency. Structured-output support varies by the underlying serving model, so the wire schema
is never trusted blindly: the response is always re-validated against the ORIGINAL ``RESPONSE_SCHEMA``
(``schema_check``) before the grounding guard, the same defense-in-depth pattern Amazon Nova's forced-tool
output goes through.

Credentials (``DATABRICKS_HOST`` / ``DATABRICKS_TOKEN``) are resolved from the process environment by the
caller (the harness/factory, which may have loaded them from ``.local/providers.env`` or
``.local/databricks.cfg``) -- this class only reads whatever is already there, the same as
``ClaudeExplanationService`` does for ``ANTHROPIC_API_KEY``. The real global ``~/.databrickscfg`` is never
read here or anywhere else in this codebase.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from app.redact import harden_sdk_logging
from app.services.explanation import failures
from app.services.explanation.base import ExplanationUnavailable
from app.services.explanation.claude import RESPONSE_SCHEMA, ClaudeExplanationService, Completion
from app.services.explanation.schema_check import validate as validate_schema
from app.terminology import Terminology

MODES = ("json_object", "prompt_only")
_STOP_REASONS = {"content_filter": "refusal"}


class DatabricksExplanationService(ClaudeExplanationService):
    def __init__(self, package_dir: Path, terminology: Terminology, endpoint: str, *, host: str | None = None,
                 token: str | None = None, timeout_seconds: float = 30.0, max_tokens: int = 4000, client=None,
                 structured_output: str | None = None):
        super().__init__(package_dir, terminology, endpoint, client=client, max_tokens=max_tokens)
        self._host = host if host is not None else os.getenv("DATABRICKS_HOST")
        self._token = token if token is not None else os.getenv("DATABRICKS_TOKEN")
        self._timeout = timeout_seconds
        self._mode = structured_output or "json_object"
        if self._mode not in MODES:
            raise ValueError(f"unsupported DATABRICKS_STRUCTURED_OUTPUT {self._mode!r} (valid: {', '.join(MODES)})")

    def _get_client(self):
        if self._client is None:
            import openai  # optional dependency ("eval" extra); the FM API is OpenAI-client-compatible

            harden_sdk_logging()
            self._client = openai.OpenAI(base_url=f"{self._host}/serving-endpoints", api_key=self._token,
                                         timeout=self._timeout)
        return self._client

    def _system_prompt(self) -> str:
        if self._mode == "prompt_only":
            return (self._system + "\n\nRespond with ONLY a single JSON object matching this schema, "
                    "no prose, no markdown fences:\n" + json.dumps(RESPONSE_SCHEMA))
        # The frozen system prompt (shared, unmodified, with every other provider) already says "Return
        # concise JSON:" -- kept here for belt-and-suspenders, but a system-message-only mention was NOT
        # enough on its own: the real diagnostic (scripts/diagnose_databricks_400.py step C) sent exactly
        # this system prompt + a plain JSON-dumped user message and still 400'd with "messages must contain
        # the word 'json'". See _user_prompt() below, which is the part that actually closes the gap.
        return self._system + "\n\nReturn only valid JSON matching the required explanation structure."

    def _user_prompt(self, model_input: dict) -> str:
        payload = json.dumps(model_input, indent=2)
        if self._mode != "json_object":
            return payload
        # The Foundation Model API (OpenAI-API-compatible) rejects `response_format: {"type": "json_object"}`
        # unless some message contains the word "json" -- and evidence from the real diagnostic run points at
        # the USER message specifically, not just any message: step B (json_object mode, a single user
        # message that said "as JSON") passed; step C (the real system prompt, which already said "JSON", +
        # a user message with none) 400'd with exactly this error. This prefix is Databricks-only --
        # RESPONSE_SCHEMA, the frozen prompt file, other providers' prompts and the model_input shape itself
        # are untouched; only a short line is added ahead of the unchanged JSON payload.
        return "Return only valid JSON matching the required explanation structure.\n\n" + payload

    def _extract_text(self, content) -> str:
        """Normalize ``choice.message.content`` into the JSON text ``json.loads()`` consumes.

        A plain string is returned unchanged -- the historical/legacy shape, and the only shape this method
        ever needs to handle for that case. Some Databricks-served models (reasoning models in particular)
        instead return content as a LIST of typed blocks; when it is a list, only its explicitly-recognized
        ``"text"``-typed blocks are extracted, in the order they appear -- a block is accepted in either
        dict form (``{"type": "text", "text": "..."}``) or SDK-object form (an object exposing ``.type``/
        ``.text`` attributes), since the ``openai`` client can return either depending on how the response
        was deserialized. Unknown/non-text block types (e.g. reasoning or tool-call blocks) are skipped
        entirely -- never serialized, never concatenated in. ``str(content)`` is never called.

        Fails closed rather than ever letting a raw ``TypeError`` from ``json.loads()`` on a non-string
        escape uncaught to ``failures.classify()``'s generic fallback (the exact bug this fixes -- see
        scripts/diagnose_databricks_p001.py's findings): an empty list, a list with no recognized text
        blocks, or any other unsupported/ambiguous shape raises ``ExplanationUnavailable(...,
        failures.INCOMPLETE)`` -- the same public category a malformed-JSON response already gets.
        """
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict):
                    block_type, text = block.get("type"), block.get("text")
                else:
                    block_type, text = getattr(block, "type", None), getattr(block, "text", None)
                if block_type == "text" and isinstance(text, str):
                    parts.append(text)
            if parts:
                return "".join(parts)
            raise ExplanationUnavailable("databricks response content list had no recognized text blocks",
                                         failures.INCOMPLETE)
        raise ExplanationUnavailable(
            f"databricks response content was an unsupported shape: {type(content).__name__}", failures.INCOMPLETE)

    def _complete(self, model_input: dict) -> Completion:
        kwargs = dict(
            model=self._model,
            max_tokens=self._max_tokens,
            messages=[{"role": "system", "content": self._system_prompt()},
                      {"role": "user", "content": self._user_prompt(model_input)}],
        )
        if self._mode == "json_object":
            kwargs["response_format"] = {"type": "json_object"}
        response = self._get_client().chat.completions.create(**kwargs)
        choice = response.choices[0]
        model = getattr(response, "model", None) or self._model
        finish = choice.finish_reason
        content = choice.message.content
        early_text = content if isinstance(content, str) else ""  # capture-only; never json.loads()'d below

        if finish in _STOP_REASONS:
            return Completion(text=early_text, stop_reason=_STOP_REASONS[finish], model=model)
        if finish == "length":
            return Completion(text=early_text, stop_reason="max_tokens", model=model)
        if finish != "stop":
            return Completion(text=early_text, stop_reason=f"finish:{finish}", model=model)

        # Adapter boundary: normalize content into JSON text, failing closed (never a raw TypeError) if the
        # shape isn't recognized -- see _extract_text()'s docstring.
        try:
            text = self._extract_text(content)
        except ExplanationUnavailable as exc:
            exc.model_name = model
            raise

        # Conservative path, mandatory: FM-API structured-output support varies by serving model, so
        # `response_format: json_object` only guarantees valid JSON -- never schema conformance.
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            unavailable = ExplanationUnavailable("databricks output was not valid JSON", failures.INCOMPLETE)
            unavailable.raw_output, unavailable.model_name = text, model
            raise unavailable from exc
        problems = validate_schema(payload, RESPONSE_SCHEMA)
        if problems:
            exc = ExplanationUnavailable("databricks output violates the response schema: " + "; ".join(problems[:3]),
                                         failures.INCOMPLETE)
            exc.raw_output, exc.model_name = text, model
            raise exc
        return Completion(text=text, stop_reason="end_turn", model=model)
