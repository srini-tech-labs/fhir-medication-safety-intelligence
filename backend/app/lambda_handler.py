"""AWS Lambda entry point (API Gateway REST proxy events) for the same FastAPI application.

    Handler: app.lambda_handler.handler

Configuration comes from environment variables only (see README / docs/PHASE4_API.md); there are no secrets: HealthLake,
DynamoDB and Bedrock all use the function's execution role.
"""
from __future__ import annotations

import logging
import os

from mangum import Mangum

from app.main import app

# Lambda's root logger defaults to WARNING; the application access log is INFO. SDK/HTTP loggers stay pinned by app.redact.
logging.getLogger().setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
logging.getLogger("mangum").setLevel(logging.WARNING)  # it logs every request path at INFO; app.access already records the route template

handler = Mangum(app, lifespan="off")
