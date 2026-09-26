"""Subprocess probe: REAL Anthropic SDK -> local fake API server, with all logging at DEBUG.

    python sdk_logging_probe.py baseline     # raw SDK client, no app hardening at all (what the SDK emits by itself)
    python sdk_logging_probe.py client-only  # the app's real client path, WITHOUT the create_app() startup hook
    python sdk_logging_probe.py hardened     # create_app() first, exactly as uvicorn does at startup

Runs in its own interpreter so import order is faithful: `anthropic` is imported lazily on the first
explanation request (after app startup), which is when ANTHROPIC_LOG=debug takes effect. No network, no real key.
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
logs: list[list] = []


class Capture(logging.Handler):
    def emit(self, record):
        logs.append([record.levelname, record.name, record.getMessage()])


root = logging.getLogger()
root.setLevel(logging.DEBUG)  # worst case: everything at DEBUG, application included
root.addHandler(Capture(logging.DEBUG))

seen = {"requests": 0}


class FakeAnthropicAPI(BaseHTTPRequestHandler):
    def do_POST(self):
        seen["requests"] += 1
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        mi = json.loads(body["messages"][0]["content"])
        payload = {
            "summary": f"The rule engine reported {mi['summaryCounts']['totalFindings']} findings.",
            "findingExplanations": [{"ruleId": f["ruleId"], "explanation": f"{f['ruleId']} ({f['severity']}): {f['finding']}"}
                                    for f in mi["deterministicFindings"]],
            "dataGapExplanation": None,
            "groundedInFindingsOnly": True,
        }
        message = {"id": "msg_probe", "type": "message", "role": "assistant", "model": body["model"],
                   "content": [{"type": "text", "text": json.dumps(payload)}], "stop_reason": "end_turn",
                   "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 5}}
        data = json.dumps(message).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.send_header("request-id", "req_probe")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):  # keep the fake server quiet
        pass


server = HTTPServer(("127.0.0.1", 0), FakeAnthropicAPI)
threading.Thread(target=server.serve_forever, daemon=True).start()

import os  # noqa: E402

os.environ["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{server.server_port}"

from app.config import Settings  # noqa: E402
from app.container import build_container  # noqa: E402

if mode == "hardened":
    from app.main import create_app  # noqa: E402

    create_app()  # installs log redaction + SDK log hardening, as the real server does at startup

base = Settings.from_env()
settings = Settings(**{**base.__dict__, "output_dir": Path(tempfile.mkdtemp()), "explanation_mode": "claude"})
if mode == "baseline":
    # Bypass the app's client factory so nothing of ours touches SDK logging: this is the SDK on its own.
    import anthropic  # noqa: E402

    from app.services.explanation.claude import ClaudeExplanationService  # noqa: E402
    from app.services.explanation.factory import SafeExplanationService  # noqa: E402
    from app.services.explanation.mock import MockExplanationService  # noqa: E402
    from app.terminology import Terminology  # noqa: E402

    claude = ClaudeExplanationService(settings.package_dir, Terminology.load(settings.package_dir),
                                      settings.explanation_model, client=anthropic.Anthropic())
    container = build_container(settings, explainer=SafeExplanationService(claude, MockExplanationService()))
else:
    container = build_container(settings)  # SDK is imported lazily inside the app on the first request
result = container.analyses.run("P008")  # P008 has a note
ai = result.ai_explanation
print(json.dumps({"mode": ai.mode if ai else None, "fallback_reason": ai.fallback_reason if ai else None,
                  "requests_seen": seen["requests"], "logs": logs}))
