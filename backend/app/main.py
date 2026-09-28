"""FastAPI application. Run: uvicorn app.main:app --reload --port 8000 (from backend/)."""
from __future__ import annotations

import json
import logging
import time

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes import router
from app.config import Settings
from app.redact import install_log_redaction, redact
from app.repository.base import StoreUnavailable

access_log = logging.getLogger("app.access")
log = logging.getLogger(__name__)


def create_app() -> FastAPI:
    install_log_redaction()
    settings = Settings.from_env()
    app = FastAPI(
        title="FHIR Medication Safety Intelligence API",
        version="1.0.0",
        description="Synthetic demonstration data. Findings come from a limited prototype rule set "
                    "and are not intended for patient care.",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )
    app.include_router(router)

    @app.exception_handler(StoreUnavailable)
    async def store_unavailable(_: Request, exc: StoreUnavailable) -> JSONResponse:
        log.warning("backing store unavailable: %s", redact(str(exc))[:300])  # cause stays in the redacted server log
        return JSONResponse(status_code=503, content={"detail": "Clinical data store temporarily unavailable"})

    @app.middleware("http")
    async def access_line(request: Request, call_next):
        """One JSON line per request: ids, route TEMPLATE, status, latency. Never bodies, headers or query strings."""
        started = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            route = request.scope.get("route")
            event = request.scope.get("aws.event") or {}  # present when served through the Lambda ASGI adapter
            access_log.info(json.dumps({
                "method": request.method, "route": getattr(route, "path", "(unmatched)"), "status": status,
                "ms": round((time.perf_counter() - started) * 1000),
                "requestId": (event.get("requestContext") or {}).get("requestId"),
            }))

    return app


app = create_app()
