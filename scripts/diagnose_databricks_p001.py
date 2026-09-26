#!/usr/bin/env python
"""Opt-in, single-patient (P001) diagnostic: pinpoint EXACTLY which post-generation stage causes every
"normal explanation" to fall back (EXPLANATION_UNAVAILABLE), now that the HTTP/model-completion layer
itself is confirmed working (the second live Databricks run: 8 successful completions, zero credential
leakage, correct truncated-output handling).

    backend/.venv/bin/python scripts/diagnose_databricks_p001.py --execute

Makes exactly ONE real, billed Databricks call, for P001 only. The SAME cached response is then replayed
(no second call) through the real, UNMODIFIED DatabricksExplanationService pipeline, so the reported
"final exception class/failure code" is what production actually raises, not a re-implementation. Default
is a dry run: prints nothing but a notice, no network, no credentials needed.

Reports ONLY, for that one response:
  - HTTP/model completion succeeded: yes/no
  - response content present: yes/no
  - JSON parse succeeded: yes/no
  - parsed top-level key names only (never values)
  - schema_check.validate() succeeded: yes/no (if not: sanitized JSON-path/schema-path problem strings --
    these already only name a path and a Python type, e.g. "$.summary: expected string, got NoneType", or a
    key name, never a value)
  - grounding guard reached: yes/no; if reached, accepted/rejected + a sanitized (redact()-passed, length
    capped) reason
  - the final exception class and public failure code DatabricksExplanationService actually raises (or
    "none: mode=llm" on success)

NEVER prints the complete generated explanation, the complete model input, credentials, or note text.
Does not change the model, endpoint, prompt, RESPONSE_SCHEMA, grounding guard, or acceptance thresholds --
diagnostic only, read-only against all of those.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "backend"))

from app.config import Settings  # noqa: E402
from app.envfile import load_databricks_cfg, load_env_file  # noqa: E402
from app.redact import redact  # noqa: E402
from app.services.explanation import failures  # noqa: E402
from app.services.explanation.base import GroundingViolation  # noqa: E402
from app.services.explanation.claude import RESPONSE_SCHEMA  # noqa: E402
from app.services.explanation.databricks import DatabricksExplanationService  # noqa: E402
from app.services.explanation.guard import build_model_input, validate_grounding  # noqa: E402
from app.services.explanation.schema_check import validate as validate_schema  # noqa: E402
from app.terminology import Terminology  # noqa: E402

LOCAL_PROVIDERS_ENV = REPO / ".local" / "providers.env"
LOCAL_DATABRICKS_CFG = REPO / ".local" / "databricks.cfg"
PATIENT = "P001"


class ReplayClient:
    """Returns the SAME already-obtained response for any call -- lets the real, unmodified
    DatabricksExplanationService pipeline run against it with zero additional network calls."""

    def __init__(self, response):
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: response))


def diagnose(settings: Settings, terms: Terminology) -> dict:
    from app.container import build_container
    from app.services.explanation.mock import MockExplanationService

    mock_settings = Settings(**{**settings.__dict__, "explanation_mode": "mock"})
    container = build_container(mock_settings, explainer=MockExplanationService())
    analysis = container.analyses.run(PATIENT).model_copy(update={"ai_explanation": None})
    snapshot = container.snapshots.snapshot(PATIENT)
    model_input = build_model_input(snapshot, analysis, None, terms)

    provider = DatabricksExplanationService(settings.package_dir, terms, settings.databricks_endpoint,
                                            max_tokens=settings.databricks_max_tokens)
    client = provider._get_client()

    report: dict = {"patient": PATIENT}

    # ---- stage 0: exactly one real call ------------------------------------------------------------------
    try:
        response = client.chat.completions.create(
            model=settings.databricks_endpoint, max_tokens=settings.databricks_max_tokens,
            messages=[{"role": "system", "content": provider._system_prompt()},
                      {"role": "user", "content": provider._user_prompt(model_input)}],
            response_format={"type": "json_object"})
    except Exception as exc:  # noqa: BLE001 - reported, sanitized, never raised raw
        report["http_model_completion_succeeded"] = False
        report["completion_error"] = f"{type(exc).__name__}: {redact(str(exc))[:200]}"
        return report
    report["http_model_completion_succeeded"] = True

    choice = response.choices[0]
    report["finish_reason"] = choice.finish_reason
    report["response_content_present"] = bool(choice.message.content)
    # The Python TYPE of `choice.message.content` (never its value): historically production did
    # `content or ""`, so if a model/SDK ever returned content as something other than a plain string (some
    # reasoning models put the answer in a different field, or return content as a list of parts), `text`
    # silently stopped being a string and json.loads(text) raised an uncaught TypeError -- collapsing to the
    # generic EXPLANATION_UNAVAILABLE instead of EXPLANATION_INCOMPLETE. Fixed: DatabricksExplanationService
    # now has its own _extract_text() normalizer, reused here (not reimplemented) so this script always
    # reflects the real, current production behavior. This field stays for visibility into the raw shape
    # without ever printing the content itself.
    report["response_content_python_type"] = type(choice.message.content).__name__

    # ---- stage 1: normalize + JSON parse (via the real DatabricksExplanationService._extract_text()) ------
    payload = None
    text = None
    try:
        text = provider._extract_text(choice.message.content)
    except Exception as exc:  # noqa: BLE001 - ExplanationUnavailable from an unrecognized/empty content shape
        report["json_parse_succeeded"] = False
        report["parsed_top_level_keys"] = None
        report["json_parse_error"] = f"{type(exc).__name__} (content normalization failed): {redact(str(exc))[:200]}"

    if text is not None:
        try:
            payload = json.loads(text)
            report["json_parse_succeeded"] = True
            report["parsed_top_level_keys"] = sorted(payload.keys()) if isinstance(payload, dict) else \
                f"<not a JSON object: {type(payload).__name__}>"
        except json.JSONDecodeError as exc:
            report["json_parse_succeeded"] = False
            report["parsed_top_level_keys"] = None
            report["json_parse_error"] = redact(str(exc))[:200]  # a position/line-column message, no content

    # ---- stage 2: schema_check.validate() -----------------------------------------------------------------
    if payload is not None:
        try:
            problems = validate_schema(payload, RESPONSE_SCHEMA)
            report["schema_check_succeeded"] = not problems
            if problems:
                report["schema_check_problems"] = [redact(p)[:200] for p in problems[:5]]  # path + type only
        except Exception as exc:  # noqa: BLE001 - e.g. UnsupportedSchema; reported, never raised raw
            report["schema_check_succeeded"] = False
            report["schema_check_problems"] = [f"{type(exc).__name__}: {redact(str(exc))[:200]}"]
            problems = ["<validate() itself raised>"]
    else:
        report["schema_check_succeeded"] = None
        problems = None

    # ---- stage 3: grounding guard -------------------------------------------------------------------------
    if payload is not None and problems == []:
        report["grounding_guard_reached"] = True
        try:
            validate_grounding(payload, model_input, terms)
            report["grounding_guard_verdict"] = "accepted"
        except GroundingViolation as exc:
            report["grounding_guard_verdict"] = "rejected"
            report["grounding_guard_reason"] = redact(str(exc))[:300]
        except Exception as exc:  # noqa: BLE001 - reported, never raised raw
            report["grounding_guard_verdict"] = "error"
            report["grounding_guard_reason"] = f"{type(exc).__name__}: {redact(str(exc))[:200]}"
    else:
        report["grounding_guard_reached"] = False

    # ---- cross-check: replay the SAME response through the real, unmodified provider pipeline -------------
    replay_provider = DatabricksExplanationService(settings.package_dir, terms, settings.databricks_endpoint,
                                                    client=ReplayClient(response), max_tokens=settings.databricks_max_tokens)
    try:
        explanation = replay_provider.explain(snapshot, analysis, None)
        report["final_exception_class"] = None
        report["final_failure_code"] = None
        report["final_mode"] = explanation.mode  # "llm" if this actually succeeded end to end
    except Exception as exc:  # noqa: BLE001 - this IS the diagnostic result, not an error in the script
        report["final_exception_class"] = type(exc).__name__
        report["final_failure_code"] = failures.classify(exc)
        report["final_mode"] = None

    return report


def print_report(report: dict) -> None:
    print(f"\n=== Databricks single-patient diagnostic ({report['patient']}) ===")
    print(f"HTTP/model completion succeeded: {'yes' if report['http_model_completion_succeeded'] else 'no'}")
    if not report["http_model_completion_succeeded"]:
        print(f"  completion error: {report['completion_error']}")
        return
    print(f"finish_reason: {report['finish_reason']}")
    print(f"response content present: {'yes' if report['response_content_present'] else 'no'}")
    print(f"JSON parse succeeded: {'yes' if report['json_parse_succeeded'] else 'no'}")
    if report["json_parse_succeeded"]:
        print(f"  parsed top-level keys: {report['parsed_top_level_keys']}")
    else:
        print(f"  JSON parse error: {report.get('json_parse_error')}")
    print(f"schema_check.validate() succeeded: "
         f"{'yes' if report['schema_check_succeeded'] else ('n/a' if report['schema_check_succeeded'] is None else 'no')}")
    if report.get("schema_check_problems"):
        print(f"  schema problems (path/type only): {report['schema_check_problems']}")
    print(f"grounding guard reached: {'yes' if report['grounding_guard_reached'] else 'no'}")
    if report["grounding_guard_reached"]:
        print(f"  guard verdict: {report['grounding_guard_verdict']}")
        if report.get("grounding_guard_reason"):
            print(f"  sanitized reason: {report['grounding_guard_reason']}")
    print(f"final exception class: {report['final_exception_class'] or '(none -- succeeded)'}")
    print(f"final public failure code: {report['final_failure_code'] or '(none -- succeeded)'}")
    if report.get("final_mode"):
        print(f"final mode: {report['final_mode']}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true",
                    help="make the one real, billed call. Without this flag: dry run, no network, no credentials needed.")
    args = ap.parse_args()

    if not args.execute:
        print("DRY RUN (no --execute given): no network call, no credentials needed.")
        print(f"Would make exactly ONE real Databricks chat-completion call for patient {PATIENT}, then report "
             "per-stage outcomes only (never the full model output or model input). Re-run with --execute.")
        return 0

    loaded = load_env_file(LOCAL_PROVIDERS_ENV)
    if loaded:
        print(f"loaded from .local/providers.env: {', '.join(loaded)}")
    loaded_cfg = load_databricks_cfg(LOCAL_DATABRICKS_CFG)
    if loaded_cfg:
        print(f"loaded from .local/databricks.cfg: {', '.join(loaded_cfg)}")

    import os
    if not os.getenv("DATABRICKS_HOST") or not os.getenv("DATABRICKS_TOKEN"):
        print("No DATABRICKS_HOST + DATABRICKS_TOKEN found. Put both in .local/providers.env, then re-run.", file=sys.stderr)
        return 2

    settings = Settings.from_env()
    terms = Terminology.load(settings.package_dir)
    report = diagnose(settings, terms)
    print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
