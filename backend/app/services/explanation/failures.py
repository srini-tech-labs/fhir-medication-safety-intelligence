"""Public, stable categories for explanation failures.

Only these codes and messages ever leave the server (API responses, saved analyses, the browser). Raw
provider exceptions (status codes, response bodies, request IDs, model names, credentials, tracebacks)
are kept out of them and appear only in redacted server logs.
"""
from __future__ import annotations

UNAVAILABLE = "EXPLANATION_UNAVAILABLE"  # auth/permission/config/unknown-model/any unclassified failure
TEMPORARY = "EXPLANATION_TEMPORARILY_UNAVAILABLE"  # rate limit, provider 5xx/overload, timeout, connection
INCOMPLETE = "EXPLANATION_INCOMPLETE"  # truncated, empty or malformed model output
DECLINED = "EXPLANATION_DECLINED"  # the model refused
REJECTED = "EXPLANATION_REJECTED"  # output failed the grounding guard

PUBLIC_MESSAGES: dict[str, str] = {
    UNAVAILABLE: "The AI explanation service is unavailable.",
    TEMPORARY: "The AI explanation service is temporarily unavailable. Please try again.",
    INCOMPLETE: "The AI explanation could not be completed.",
    DECLINED: "The AI service did not produce an explanation for this request.",
    REJECTED: "The AI explanation was withheld because it did not pass the grounding checks.",
}

_TRANSIENT_NAMES = {"APIConnectionError", "APITimeoutError", "TimeoutError", "ConnectionError"}


def public_reason(code: str) -> str:
    return PUBLIC_MESSAGES[code]


def classify(exc: BaseException) -> str:
    """Map any exception to one public code. Duck-typed, so the SDK need not be importable."""
    code = getattr(exc, "public_code", None)
    if code in PUBLIC_MESSAGES:
        return code
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return TEMPORARY if status in (408, 409, 429) or status >= 500 else UNAVAILABLE
    if _TRANSIENT_NAMES & {c.__name__ for c in type(exc).__mro__}:
        return TEMPORARY
    return UNAVAILABLE
