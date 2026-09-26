"""Subprocess probe: REAL `google-genai` SDK -> local fake API server, with all logging at DEBUG.

    python sdk_logging_probe_gemini.py baseline     # raw SDK client, no app hardening at all
    python sdk_logging_probe_gemini.py client-only  # the app's real client path, WITHOUT create_app()
    python sdk_logging_probe_gemini.py hardened     # create_app() first, exactly as uvicorn does at startup

Points the SDK at a local fake server via GeminiExplanationService's `base_url` override (an
`http_options.base_url` pass-through, testing/gateway-only -- see gemini.py). Runs in its own interpreter so
import order is faithful. No network, no real key.
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


class FakeGeminiAPI(BaseHTTPRequestHandler):
    def do_POST(self):
        seen["requests"] += 1
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        contents = body.get("contents") or []
        text_in = "{}"
        if contents and contents[0].get("parts"):
            text_in = contents[0]["parts"][0].get("text", "{}")
        try:
            mi = json.loads(text_in)
            counts = mi["summaryCounts"]["totalFindings"]
            findings = mi["deterministicFindings"]
        except Exception:
            counts, findings = 0, []
        payload = {
            "summary": f"The rule engine reported {counts} findings.",
            "findingExplanations": [{"ruleId": f["ruleId"], "explanation": f"{f['ruleId']} ({f['severity']}): {f['finding']}"}
                                    for f in findings],
            "dataGapExplanation": None,
            "groundedInFindingsOnly": True,
        }
        response = {
            "candidates": [{"content": {"parts": [{"text": json.dumps(payload)}], "role": "model"},
                           "finishReason": "STOP", "index": 0}],
            "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5},
            "modelVersion": "gemini-3.8-flash",
        }
        data = json.dumps(response).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.send_header("request-id", "req_probe")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):  # keep the fake server quiet
        pass


server = HTTPServer(("127.0.0.1", 0), FakeGeminiAPI)
threading.Thread(target=server.serve_forever, daemon=True).start()
base_url = f"http://127.0.0.1:{server.server_port}"

import os  # noqa: E402

from app.config import Settings  # noqa: E402
from app.container import build_container  # noqa: E402

if mode == "hardened":
    from app.main import create_app  # noqa: E402

    create_app()  # installs log redaction + SDK log hardening, as the real server does at startup

base = Settings.from_env()
settings = Settings(**{**base.__dict__, "output_dir": Path(tempfile.mkdtemp()), "explanation_mode": "claude",
                       "explanation_provider": "gemini", "explanation_model": "gemini-3.8-flash"})

if mode == "baseline":
    # Bypass the app's client factory so nothing of ours touches SDK logging: this is the SDK on its own.
    from google import genai  # noqa: E402

    from app.services.explanation.factory import SafeExplanationService  # noqa: E402
    from app.services.explanation.gemini import GeminiExplanationService  # noqa: E402
    from app.services.explanation.mock import MockExplanationService  # noqa: E402
    from app.terminology import Terminology  # noqa: E402

    real_client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"),
                               http_options=genai.types.HttpOptions(base_url=base_url))
    provider = GeminiExplanationService(settings.package_dir, Terminology.load(settings.package_dir),
                                        settings.explanation_model, client=real_client)
    container = build_container(settings, explainer=SafeExplanationService(provider, MockExplanationService()))
else:
    # The client-only/hardened paths build their client through the normal factory path; GEMINI_BASE_URL redirects
    # the real SDK at the fake server without any app/Settings wiring (see gemini.py: GeminiExplanationService reads
    # it directly, the same convention ANTHROPIC_BASE_URL/OPENAI_BASE_URL already use for their SDKs).
    os.environ["GEMINI_BASE_URL"] = base_url
    container = build_container(settings)  # SDK is imported lazily inside the app on the first request
result = container.analyses.run("P008")  # P008 has a note
ai = result.ai_explanation
print(json.dumps({"mode": ai.mode if ai else None, "fallback_reason": ai.fallback_reason if ai else None,
                  "requests_seen": seen["requests"], "logs": logs}))
