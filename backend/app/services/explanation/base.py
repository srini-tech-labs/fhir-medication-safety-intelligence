"""AI explanation layer interface.

The explanation layer receives *finished* deterministic results and may only describe them. It
never decides whether a risk exists, changes severity, or adds findings.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from app.models.contract import AIExplanation, Analysis, Snapshot
from app.services.explanation import failures


class ExplanationService(ABC):
    @abstractmethod
    def explain(
        self,
        patient_snapshot: Snapshot,
        deterministic_analysis: Analysis,
        note_context: str | None = None,
    ) -> AIExplanation:
        """``deterministic_analysis`` is complete (findings, gaps, overall) before this is called."""


class ExplanationError(RuntimeError):
    """Base for failures whose public category is known. The message is for server logs only.

    When the failure concerns a model completion, ``raw_output`` holds the exact text the model returned and
    ``flagged`` the sentences that triggered content categories, so a rejection can be reviewed. These are captured to the
    local diagnostic store (see SafeExplanationService) -- never logged, never sent to API clients.
    """

    public_code: str = failures.UNAVAILABLE

    def __init__(self, message: str = ""):
        super().__init__(message)
        self.raw_output: str | None = None
        self.model_name: str | None = None
        self.flagged: list[dict] = []


class GroundingViolation(ExplanationError):
    """Model output referenced something that was not in the supplied deterministic input."""

    public_code = failures.REJECTED

    def __init__(self, message: str, flagged: list[dict] | None = None):
        super().__init__(message)
        self.flagged = list(flagged or [])


class ExplanationUnavailable(ExplanationError):
    """The model did not return a usable completion (refusal, truncation, empty or malformed output)."""

    def __init__(self, message: str, public_code: str = failures.INCOMPLETE):
        super().__init__(message)
        self.public_code = public_code
