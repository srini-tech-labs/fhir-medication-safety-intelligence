"""Scope guards. Phase 3 added AWS HealthLake as a second backend; Phase 4 adds the Lambda entry point, DynamoDB app state and the
Bedrock Converse explanation provider. Everything else stays out of scope.

* valid backends are exactly `local` and `healthlake` (S3 is staging for import only, never an application backend);
* HealthLake/AWS SDK code is confined to the repository adapters, the Bedrock provider, config and factories -- the rules, models
  and API routes stay backend-agnostic;
* Lambda / Mangum / Bedrock / DynamoDB references are confined to the files that need them;
* SMART on FHIR, Athena/Lake Formation, Data Transformation, `$export`, HealthLake NLP and the Mantle endpoint do not appear.
"""
from __future__ import annotations

import re

import pytest

from app.config import REPO_ROOT, VALID_BACKENDS, Settings
from app.repository.factory import create_repository

APP = REPO_ROOT / "backend" / "app"
ALLOWED_AWS_FILES = {
    "repository/healthlake.py", "repository/healthlake_client.py", "repository/app_state.py", "repository/app_state_dynamodb.py",
    "repository/factory.py", "config.py", "redact.py",  # redact.py only pins AWS SDK loggers
    "services/explanation/bedrock.py", "services/explanation/factory.py", "lambda_handler.py",
    "secrets.py",  # Secrets Manager fetch for OPENAI_API_KEY in the deployed Lambda only (docs/adr/0011)
}
PHASE4_FILES = {
    "lambda_handler.py", "repository/app_state_dynamodb.py", "repository/healthlake.py", "repository/factory.py", "services/explanation/bedrock.py",
    "services/explanation/factory.py", "config.py", "redact.py", "secrets.py",
}
PHASE4_TERMS = re.compile(r"mangum|bedrock|dynamodb|lambda_handler|api ?gateway|aws lambda", re.IGNORECASE)
AWS_TERMS = re.compile(r"healthlake|boto3|botocore|sigv4|aws_", re.IGNORECASE)
FORBIDDEN_TERMS = re.compile(r"smart[- ]on[- ]fhir|cognito|athena|lake ?formation|\$export|data transformation|comprehend ?medical|bedrock-mantle|BedrockMantle", re.IGNORECASE)


def test_valid_backends_are_local_and_healthlake():
    assert VALID_BACKENDS == ("local", "healthlake")


def test_s3_is_not_an_application_backend(settings):
    bad = Settings(**{**settings.__dict__, "data_backend": "s3"})
    with pytest.raises(ValueError, match="Unsupported DATA_BACKEND"):
        create_repository(bad)


def test_default_backend_stays_local(monkeypatch):
    monkeypatch.delenv("DATA_BACKEND", raising=False)
    assert Settings.from_env().data_backend == "local"


def test_aws_code_is_confined_to_the_repository_adapter_config_and_factory():
    offenders = sorted(
        str(p.relative_to(APP)) for p in APP.rglob("*.py")
        if str(p.relative_to(APP)) not in ALLOWED_AWS_FILES and AWS_TERMS.search(p.read_text("utf-8"))
    )
    assert offenders == []


def test_business_logic_never_imports_the_aws_stack():
    """Rules, models, API routes and the non-provider services never import boto3/httpx; the Bedrock provider is the one adapter that may."""
    adapters = {APP / "services" / "explanation" / "bedrock.py"}
    for sub in ("rules", "services", "models", "api"):
        for p in (APP / sub).rglob("*.py"):
            if p in adapters:
                continue
            text = p.read_text("utf-8")
            assert not re.search(r"^\s*(import|from)\s+(boto3|botocore|httpx)", text, re.M), p


def test_phase4_infrastructure_references_are_confined_to_the_files_that_need_them():
    offenders = sorted(str(p.relative_to(APP)) for p in APP.rglob("*.py")
                       if str(p.relative_to(APP)) not in PHASE4_FILES and PHASE4_TERMS.search(p.read_text("utf-8")))
    assert offenders == []


def test_features_that_are_still_out_of_scope_do_not_appear_in_application_code():
    offenders = sorted(str(p.relative_to(APP)) for p in APP.rglob("*.py") if FORBIDDEN_TERMS.search(p.read_text("utf-8")))
    assert offenders == []


def test_the_bedrock_provider_uses_converse_on_bedrock_runtime_not_the_mantle_endpoint():
    text = (APP / "services" / "explanation" / "bedrock.py").read_text("utf-8")
    assert "converse(" in text and "bedrock-runtime" in text and "AnthropicBedrock" not in text and "mantle" not in text.lower()


def test_lambda_code_paths_never_write_fhir():
    """Phase 4 is read-only against HealthLake: the write switch defaults off and the handler never sets it."""
    assert Settings.from_env().healthlake_write_outputs is False
    assert "healthlake_write_outputs" not in (APP / "lambda_handler.py").read_text("utf-8").lower()


def test_local_repository_does_not_depend_on_the_aws_extra():
    """The default local run must work without boto3/httpx installed (they live in the optional `aws` extra)."""
    import subprocess, sys

    code = ("import sys; sys.modules['boto3']=None; sys.modules['botocore']=None; sys.modules['httpx']=None; "
            "import app.repository.factory, app.container; print('ok')")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=REPO_ROOT / "backend")
    assert out.returncode == 0 and "ok" in out.stdout, out.stderr[-500:]
