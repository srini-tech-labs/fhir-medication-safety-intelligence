"""Claude / Amazon Nova on Amazon Bedrock Runtime (Converse API) -- same prompt, schema, guard and fallbacks as the Claude API provider.

Only the transport differs: one ``converse`` call. Everything after it -- stop-reason handling, JSON parsing, the grounding guard,
public fallback categories, captured rejections -- is inherited from ``ClaudeExplanationService``.

Two documented ways to get schema-shaped output out of Converse, chosen by model family (never guessed):

* ``tool``        Amazon Nova. The explanation schema is offered as ONE forced tool (``toolConfig`` + ``toolChoice.tool``); the result is
                  ``toolUse.input``. Nova documents that a tool's top-level input schema supports only ``type``/``properties``/``required``,
                  so the *generation* schema is a compatible derivation of ``RESPONSE_SCHEMA`` (no ``additionalProperties``, no ``anyOf``)
                  and the output is then re-validated against the ORIGINAL ``RESPONSE_SCHEMA`` (``schema_check``) before the guard.
* ``text_format`` Anthropic Claude. ``outputConfig.textFormat`` + ``outputConfig.effort`` (paused: Claude is not deployed in Phase 4).

Reasoning is never enabled: Nova gets an explicit ``reasoningConfig: disabled`` (also its default). A rejected schema/parameter is
NOT worked around: it surfaces as a labelled fallback and is reported. Auth: the execution role's credentials (SigV4 via boto3).
"""
from __future__ import annotations

import copy
import json
import logging
from pathlib import Path

from app.redact import harden_sdk_logging, redact
from app.services.explanation import failures
from app.services.explanation.base import ExplanationUnavailable
from app.services.explanation.claude import RESPONSE_SCHEMA, ClaudeExplanationService, Completion
from app.services.explanation.schema_check import validate as validate_schema
from app.terminology import Terminology

log = logging.getLogger(__name__)

SCHEMA_NAME = "medication_safety_explanation"
SCHEMA_DESCRIPTION = "Grounded explanation of deterministic medication-safety findings"
TOOL_NAME = "record_explanation"
TOOL_DESCRIPTION = ("Record the grounded explanation of the deterministic medication-safety findings supplied in the user message. "
                    "Describe only the supplied findings and data gaps; never add findings, values, doses or advice.")
MODES = ("tool", "text_format")
NOVA_MAX_OUTPUT_TOKENS = 5000  # documented Converse request limit for Nova (one AWS page; another example uses more -> stay inside)
NOVA_TEMPERATURE = 0.00001     # documented range is 0.00001-1 (near-greedy); 0 is outside it
NOVA_TOP_LEVEL_KEYS = {"type", "properties", "required"}  # documented: all a Nova tool's top-level input schema supports
# Converse stopReason -> the vocabulary of ClaudeExplanationService._finish
_STOP_REASONS = {"content_filtered": "refusal", "guardrail_intervened": "refusal"}
_TRANSIENT_CODES = {"ThrottlingException", "ModelTimeoutException", "ServiceUnavailableException", "InternalServerException",
                    "ModelNotReadyException", "RequestTimeout", "TooManyRequestsException"}


def default_mode(model_id: str) -> str:
    """The structured-output mode for a model family. Unknown families are an error, not a guess."""
    family = model_id.lower()
    if "amazon.nova" in family:
        return "tool"
    if "anthropic." in family:
        return "text_format"
    raise ValueError(f"cannot choose a structured-output mode for model {model_id!r}: set BEDROCK_STRUCTURED_OUTPUT to one of {MODES}")


def generation_schema(schema: dict) -> dict:
    """A constrained-decoding-compatible derivation of the contract schema: drops ``additionalProperties`` (unsupported by Nova) and
    rewrites ``anyOf[X, null]`` as ``type: [X, null]``. It may accept MORE than the contract; the contract is re-applied afterwards."""
    def convert(node):
        if isinstance(node, list):
            return [convert(n) for n in node]
        if not isinstance(node, dict):
            return node
        node = dict(node)
        node.pop("additionalProperties", None)
        alternatives = node.pop("anyOf", None)
        if alternatives is not None:
            simple = [a.get("type") for a in alternatives if isinstance(a, dict) and set(a) == {"type"} and isinstance(a["type"], str)]
            if len(simple) != len(alternatives):
                raise ValueError("anyOf with non-trivial alternatives cannot be expressed as a type list")
            node["type"] = simple
        return {k: convert(v) for k, v in node.items()}

    out = convert(copy.deepcopy(schema))
    if set(out) - NOVA_TOP_LEVEL_KEYS:
        raise ValueError(f"generation schema has top-level keys beyond {sorted(NOVA_TOP_LEVEL_KEYS)}: {sorted(set(out) - NOVA_TOP_LEVEL_KEYS)}")
    return out


NOVA_TOOL_SCHEMA = generation_schema(RESPONSE_SCHEMA)


class BedrockClaudeExplanationService(ClaudeExplanationService):
    def __init__(self, package_dir: Path, terminology: Terminology, model: str, *, region: str = "us-east-1",
                 timeout_seconds: float = 20.0, max_tokens: int = 4000, client=None, structured_output: str | None = None):
        super().__init__(package_dir, terminology, model, client=client, max_tokens=max_tokens)
        self._region, self._timeout = region, timeout_seconds
        self._mode = structured_output or default_mode(model)
        if self._mode not in MODES:
            raise ValueError(f"unsupported BEDROCK_STRUCTURED_OUTPUT {self._mode!r} (valid: {', '.join(MODES)})")
        if self._mode == "tool" and max_tokens > NOVA_MAX_OUTPUT_TOKENS:
            raise ValueError(f"maxTokens {max_tokens} exceeds the documented Nova limit of {NOVA_MAX_OUTPUT_TOKENS}")

    def _get_client(self):
        if self._client is None:
            import boto3
            from botocore.config import Config

            harden_sdk_logging()  # botocore DEBUG logs canonical requests and payload handling
            self._client = boto3.client(
                "bedrock-runtime", region_name=self._region,
                config=Config(connect_timeout=3, read_timeout=self._timeout, retries={"total_max_attempts": 1, "mode": "standard"}),
            )
        return self._client

    # ---- request -------------------------------------------------------------------------------------------------
    def _request(self, model_input: dict) -> dict:
        request = {
            "modelId": self._model,
            "system": [{"text": self._system}],
            "messages": [{"role": "user", "content": [{"text": json.dumps(model_input, indent=2)}]}],
        }
        if self._mode == "tool":
            request["inferenceConfig"] = {"maxTokens": self._max_tokens, "temperature": NOVA_TEMPERATURE}  # topP left unset
            request["toolConfig"] = {
                "tools": [{"toolSpec": {"name": TOOL_NAME, "description": TOOL_DESCRIPTION, "inputSchema": {"json": NOVA_TOOL_SCHEMA}}}],
                "toolChoice": {"tool": {"name": TOOL_NAME}},
            }
            request["additionalModelRequestFields"] = {"reasoningConfig": {"type": "disabled"}}  # an explanation task, not clinical reasoning
        else:
            request["inferenceConfig"] = {"maxTokens": self._max_tokens}
            request["outputConfig"] = {
                "effort": "low",
                "textFormat": {"type": "json_schema", "structure": {"jsonSchema": {
                    "schema": json.dumps(RESPONSE_SCHEMA), "name": SCHEMA_NAME, "description": SCHEMA_DESCRIPTION}}},
            }
        return request

    # ---- call + response -----------------------------------------------------------------------------------------------
    def _converse(self, model_input: dict) -> dict:
        try:
            return self._get_client().converse(**self._request(model_input))
        except ExplanationUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - botocore ClientError / timeouts / connection errors
            code = ((getattr(exc, "response", None) or {}).get("Error") or {}).get("Code", "")  # `response` may be None (timeouts)
            transient = code in _TRANSIENT_CODES or type(exc).__name__ in {
                "ReadTimeoutError", "ConnectTimeoutError", "EndpointConnectionError", "ConnectionClosedError"}
            # the AWS message may name a rejected schema keyword or parameter (needed to report it); it never contains our prompt
            detail = redact(str(exc))[:240]
            raise ExplanationUnavailable(f"bedrock converse failed: {code or type(exc).__name__}: {detail}",
                                         failures.TEMPORARY if transient else failures.UNAVAILABLE) from exc

    def _complete(self, model_input: dict) -> Completion:
        response = self._converse(model_input)
        blocks = (response.get("output") or {}).get("message", {}).get("content", [])
        stop = response.get("stopReason", "")
        usage = response.get("usage") or {}
        log.info("bedrock converse mode=%s stop=%s in=%s out=%s ms=%s", self._mode, stop, usage.get("inputTokens"), usage.get("outputTokens"),
                 (response.get("metrics") or {}).get("latencyMs"))
        if self._mode == "tool":
            return self._from_tool_use(blocks, stop)
        text = "".join(b["text"] for b in blocks if "text" in b)  # reasoning blocks are ignored
        return Completion(text=text, stop_reason=_STOP_REASONS.get(stop, stop), model=self._model)

    def _from_tool_use(self, blocks: list, stop: str) -> Completion:
        """Forced-tool result -> the serialized explanation, after re-validating it against the ORIGINAL contract schema."""
        if stop in _STOP_REASONS:
            return Completion(text="", stop_reason=_STOP_REASONS[stop], model=self._model)
        tool = next((b["toolUse"] for b in blocks if "toolUse" in b and b["toolUse"].get("name") == TOOL_NAME), None)
        if stop != "tool_use" or tool is None:
            # text-only answer, wrong/missing tool, max_tokens, malformed_tool_use, malformed_model_output, ...: incomplete, raw text kept for review
            text = "".join(b["text"] for b in blocks if "text" in b)
            return Completion(text=text, stop_reason=f"no_tool_use:{stop or 'none'}", model=self._model)
        text = json.dumps(tool["input"])
        problems = validate_schema(tool["input"], RESPONSE_SCHEMA)
        if problems:
            exc = ExplanationUnavailable("tool input violates the response schema: " + "; ".join(problems[:3]), failures.INCOMPLETE)
            exc.raw_output, exc.model_name = text, self._model  # captured for review, never logged
            raise exc
        return Completion(text=text, stop_reason="end_turn", model=self._model)
