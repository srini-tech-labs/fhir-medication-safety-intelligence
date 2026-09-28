"""Subprocess probe: REAL `openai` SDK -> local fake API server, with all logging at DEBUG.

Covers both the OpenAI and Databricks providers, which share the `openai` SDK/HTTP stack -- select which
with argv[2] (default "openai"). Unlike the Anthropic SDK, `openai` has no documented `*_LOG=debug` env var
that re-applies debug logging on import; DEBUG propagates from the root logger to its child loggers by
default (Python `logging` NOTSET inheritance), which is what "baseline" (no app hardening) exercises here.

    python sdk_logging_probe_openai.py baseline     openai       # raw SDK client, no app hardening at all
    python sdk_logging_probe_openai.py client-only  openai       # the app's real client path, WITHOUT create_app()
    python sdk_logging_probe_openai.py hardened     databricks   # create_app() first, exactly as uvicorn does

Runs in its own interpreter so import order is faithful. No network, no real key.
Prints one JSON object: {"mode", "fallback_reason", "requests_seen", "logs": [[level, logger, message], ...]}.
"""
from __future__ import annotations

import json
import logging
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend"))

mode = sys.argv[1]
provider = sys.argv[2] if len(sys.argv) > 2 else "openai"
logs: list[list] = []


class Capture(logging.Handler):
    def emit(self, record):
        logs.append([record.levelname, record.name, record.getMessage()])


root = logging.getLogger()
root.setLevel(logging.DEBUG)  # worst case: everything at DEBUG, application included
root.addHandler(Capture(logging.DEBUG))

seen = {"requests": 0}


def explanation_payload(model_input: dict) -> dict:
    return {
        "summary": f"The rule engine reported {model_input['summaryCounts']['totalFindings']} findings.",
        "findingExplanations": [{"ruleId": f["ruleId"], "explanation": f"{f['ruleId']} ({f['severity']}): {f['finding']}"}
                                for f in model_input["deterministicFindings"]],
        "dataGapExplanation": None,
        "groundedInFindingsOnly": True,
    }


class FakeOpenAICompatibleAPI(BaseHTTPRequestHandler):
    def do_POST(self):
        seen["requests"] += 1
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        if self.path.startswith("/responses"):
            model_input = json.loads(body["input"])
            response = {"id": "resp_probe", "object": "response", "status": "completed", "model": body["model"],
                       "output": [{"type": "message", "role": "assistant",
                                  "content": [{"type": "output_text", "text": json.dumps(explanation_payload(model_input))}]}],
                       "usage": {"input_tokens": 10, "output_tokens": 5}}
        else:  # /chat/completions (Databricks Foundation Model API surface)
            # The user message may be prefixed with a short "Return only valid JSON..." instruction ahead of
            # the JSON payload (DatabricksExplanationService._user_prompt(), json_object mode) -- parse from
            # the first '{' so this fake server accepts both the prefixed and unprefixed (prompt_only) shape.
            content = body["messages"][-1]["content"]
            model_input = json.loads(content[content.index("{"):])
            response = {"id": "chatcmpl_probe", "object": "chat.completion", "model": body["model"],
                       "choices": [{"index": 0, "finish_reason": "stop",
                                    "message": {"role": "assistant", "content": json.dumps(explanation_payload(model_input))}}],
                       "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
        data = json.dumps(response).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.send_header("request-id", "req_probe")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):  # keep the fake server quiet
        pass


server = HTTPServer(("127.0.0.1", 0), FakeOpenAICompatibleAPI)
threading.Thread(target=server.serve_forever, daemon=True).start()
base_url = f"http://127.0.0.1:{server.server_port}"

import os  # noqa: E402

if provider == "openai":
    os.environ["OPENAI_BASE_URL"] = base_url
else:
    os.environ["DATABRICKS_HOST"] = base_url  # DatabricksExplanationService appends /serving-endpoints itself

from app.config import Settings  # noqa: E402
from app.container import build_container  # noqa: E402

if mode == "hardened":
    from app.main import create_app  # noqa: E402

    create_app()  # installs log redaction + SDK log hardening, as the real server does at startup

overrides = {"output_dir": Path(tempfile.mkdtemp()), "explanation_mode": "claude", "explanation_provider": provider}
if provider == "openai":
    overrides["explanation_model"] = "gpt-5.6-luna"
else:
    overrides["databricks_endpoint"] = "probe-endpoint"
base = Settings.from_env()
settings = Settings(**{**base.__dict__, **overrides})

if mode == "baseline":
    # Bypass the app's client factory so nothing of ours touches SDK logging: this is the SDK on its own.
    import openai  # noqa: E402

    from app.services.explanation.factory import SafeExplanationService  # noqa: E402
    from app.services.explanation.mock import MockExplanationService  # noqa: E402
    from app.terminology import Terminology  # noqa: E402

    terms = Terminology.load(settings.package_dir)
    if provider == "openai":
        from app.services.explanation.openai import OpenAIExplanationService  # noqa: E402

        real = OpenAIExplanationService(settings.package_dir, terms, settings.explanation_model, client=openai.OpenAI())
    else:
        from app.services.explanation.databricks import DatabricksExplanationService  # noqa: E402

        real = DatabricksExplanationService(settings.package_dir, terms, settings.databricks_endpoint, host=base_url,
                                            token="fake-not-real", client=openai.OpenAI(base_url=f"{base_url}/serving-endpoints", api_key="fake-not-real"))
    container = build_container(settings, explainer=SafeExplanationService(real, MockExplanationService()))
else:
    container = build_container(settings)  # SDK is imported lazily inside the app on the first request

if mode == "hardened":
    # Reproduce the exact real-harness sequence (scripts/live_check_lib.py's `run()`, which scripts/live_check.py's
    # main() feeds into -- both call install_log_redaction() once, up front, exactly like create_app() does):
    # hardening pins the SDK loggers to WARNING above, then something re-lowers them back to DEBUG before the
    # request -- exactly what live_check_lib.run() deliberately does as its own "what if debug logging gets
    # re-enabled" stress test, and exactly what happened for real on the first live Databricks run. Without this
    # re-lowering step here, this probe never exercises that scenario at all (httpx's own INFO-level
    # request-line logger stays pinned to WARNING and never fires), which is why the credential-leak regression
    # this probe guards against was not caught until a real run hit it.
    #
    # The client is normally built LAZILY on first use, and _get_client() calls harden_sdk_logging() again at
    # that point -- which would silently re-pin the loggers back to WARNING, undoing the re-lowering below,
    # right before the real request. The real harness never hits this because it always constructs and injects
    # the client explicitly (client=...) before any re-lowering happens, so _get_client()'s own hardening call
    # never fires a second time. Force that same "already constructed" state here by touching _get_client() once
    # now (builds the openai.OpenAI object; no network call).
    #
    # NOT applied to "client-only": that mode deliberately never calls install_log_redaction() at all (only
    # _get_client()'s own harden_sdk_logging(), i.e. level-pinning with no record-suppression factory installed)
    # -- representing a bare script/worker that skips full app bootstrap. Re-lowering the level there would only
    # prove that skipping install_log_redaction() removes its own protection, which is a different, narrower
    # finding, not the scenario any real caller in this project (live_check.py, live_claude_check.py, the app
    # itself) actually goes through -- all of them call install_log_redaction() up front.
    container.explainer._primary._get_client()
    for name in ("openai", "httpx", "httpx2", "httpcore"):
        logging.getLogger(name).setLevel(logging.DEBUG)

result = container.analyses.run("P008")  # P008 has a note
ai = result.ai_explanation
print(json.dumps({"mode": ai.mode if ai else None, "fallback_reason": ai.fallback_reason if ai else None,
                  "requests_seen": seen["requests"], "logs": logs}))
