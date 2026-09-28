"""Claude-backed explanation service (used only when credentials are configured).

The model receives finished deterministic results plus the frozen guardrail system prompt
(data/phase0_v1_0/prompts/ai_explanation_system.txt), returns schema-constrained JSON, and the
output is then checked by ``guard.validate_grounding`` before it is accepted.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from app.models.contract import AIExplanation, Analysis, FindingExplanation, Snapshot
from app.redact import harden_sdk_logging
from app.services.explanation import failures
from app.services.explanation.base import ExplanationError, ExplanationService, ExplanationUnavailable
from app.services.explanation.guard import build_model_input, validate_grounding
from app.terminology import Terminology

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "findingExplanations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"ruleId": {"type": "string"}, "explanation": {"type": "string"}},
                "required": ["ruleId", "explanation"],
                "additionalProperties": False,
            },
        },
        "dataGapExplanation": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "groundedInFindingsOnly": {"type": "boolean"},
    },
    "required": ["summary", "findingExplanations", "dataGapExplanation", "groundedInFindingsOnly"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class Completion:
    """One finished model call, normalised across providers (``stop_reason`` uses the Claude API vocabulary)."""
    text: str
    stop_reason: str
    model: str


class ClaudeExplanationService(ExplanationService):
    def __init__(self, package_dir: Path, terminology: Terminology, model: str, client=None,
                 max_tokens: int = 16000):
        self._system = (package_dir / "prompts" / "ai_explanation_system.txt").read_text("utf-8")
        self._terms = terminology
        self._model = model
        self._max_tokens = max_tokens
        self._client = client  # injectable for tests; created lazily so the SDK stays optional

    def _get_client(self):
        if self._client is None:
            import anthropic  # optional dependency ("llm" extra)

            harden_sdk_logging()  # importing the SDK re-applies ANTHROPIC_LOG=debug; its DEBUG logs dump request bodies
            self._client = anthropic.Anthropic()  # resolves credentials from the environment
        return self._client

    def _complete(self, model_input: dict) -> "Completion":
        """One model call on the Claude API. Providers override only this (and the client construction)."""
        response = self._get_client().messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            system=self._system,
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": RESPONSE_SCHEMA}},
            messages=[{"role": "user", "content": json.dumps(model_input, indent=2)}],
        )
        text = next((b.text for b in response.content if b.type == "text"), "")
        return Completion(text=text, stop_reason=response.stop_reason, model=getattr(response, "model", self._model))

    def explain(
        self,
        patient_snapshot: Snapshot,
        deterministic_analysis: Analysis,
        note_context: str | None = None,
    ) -> AIExplanation:
        model_input = build_model_input(patient_snapshot, deterministic_analysis, note_context, self._terms)
        completion = self._complete(model_input)
        return self._finish(completion, model_input)

    def _finish(self, completion: "Completion", model_input: dict) -> AIExplanation:
        """Provider-independent: stop-reason handling, JSON parsing, the grounding guard, and the AIExplanation."""
        text, model = completion.text, completion.model
        try:
            if completion.stop_reason == "refusal":
                raise ExplanationUnavailable("model refused the request", failures.DECLINED)
            if completion.stop_reason != "end_turn":  # e.g. "max_tokens"
                raise ExplanationUnavailable(f"model stopped with stop_reason={completion.stop_reason!r}")
            try:
                payload = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ExplanationUnavailable("model output was not valid JSON") from exc
            if not isinstance(payload, dict):
                raise ExplanationUnavailable("model output was not a JSON object")

            validate_grounding(payload, model_input, self._terms)  # raises GroundingViolation
        except ExplanationError as exc:
            exc.raw_output, exc.model_name = text, model  # exact model text, kept for review (captured, never logged)
            raise

        explanations = [FindingExplanation(rule_id=e["ruleId"], explanation=e["explanation"])
                        for e in payload["findingExplanations"]]
        gap_text = payload["dataGapExplanation"] or None
        parts = [payload["summary"], *(e.explanation for e in explanations)] + ([gap_text] if gap_text else [])
        return AIExplanation(
            text="\n\n".join(parts),
            summary=payload["summary"],
            finding_explanations=explanations,
            data_gap_explanation=gap_text,
            grounded_in_findings_only=True,
            mode="llm",
            model=model,
        )
