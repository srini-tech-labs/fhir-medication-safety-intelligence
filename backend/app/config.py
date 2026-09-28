"""Runtime configuration, read from environment variables (see README)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# The Phase 0 dataset is frozen at this date. Ages and the DG-001 90-day lookback are computed
# relative to it (not the wall clock) so results are reproducible; override with ANALYSIS_AS_OF_DATE.
DEFAULT_AS_OF_DATE = date(2026, 9, 18)

VALID_BACKENDS = ("local", "healthlake")
VALID_APP_STATE_BACKENDS = ("local", "dynamodb")
VALID_EXPLANATION_PROVIDERS = ("anthropic", "bedrock", "openai", "gemini", "databricks")


@dataclass(frozen=True)
class Settings:
    data_backend: str
    package_dir: Path
    output_dir: Path
    as_of_date: date
    cors_origins: tuple[str, ...]
    explanation_mode: str  # "auto" | "mock" | "claude" (legacy name, provider-agnostic: see factory.py's docstring)
    explanation_model: str
    # Keep the exact rejected model output + category in the local output store for review (never logged / never sent to clients).
    capture_rejected_explanations: bool = True
    # AWS HealthLake backend (DATA_BACKEND=healthlake). SigV4 via the default AWS credential chain, or assume `healthlake_role_arn`.
    healthlake_datastore_id: str | None = None
    healthlake_region: str = "us-east-1"
    healthlake_role_arn: str | None = None
    healthlake_endpoint: str | None = None  # override (tests / VPC endpoint); default is the regional public endpoint
    # Deployment-level kill switch for the explicit FHIR persist endpoint only (POST .../persist) -- analyze()/
    # explain() never write to the clinical store regardless of this value. Off by default.
    healthlake_write_outputs: bool = False
    # Application state (analyses, rejected-output captures) for the HealthLake backend: `local` files or shared `dynamodb` (Lambda).
    app_state_backend: str = "local"
    app_state_table: str = "medsafety-app-state"
    # AI explanation provider: the Claude API (`anthropic`, default) or Amazon Bedrock Runtime Converse (`bedrock`).
    explanation_provider: str = "anthropic"
    bedrock_region: str = "us-east-1"
    bedrock_timeout_seconds: float = 20.0
    bedrock_max_tokens: int = 4000
    bedrock_structured_output: str | None = None  # `tool` (Amazon Nova) | `text_format` (Claude); None = chosen from the model family
    # OpenAI (Responses API): the initial DEPLOYED cloud provider (see docs/adr/0011). Google Gemini remains an
    # evaluation candidate only. Credentials (OPENAI_API_KEY / GEMINI_API_KEY) are read directly from the
    # environment by the provider, same as ANTHROPIC_API_KEY -- in Lambda, OPENAI_API_KEY is never set directly;
    # openai_api_key_secret_arn names a Secrets Manager secret that backend/app/secrets.py fetches into that same
    # env var once, on first use (see factory.py). Never .local/providers.env, never packaged into the Lambda zip.
    openai_timeout_seconds: float = 20.0
    openai_max_tokens: int = 4000
    openai_api_key_secret_arn: str | None = None
    gemini_timeout_seconds: float = 20.0
    gemini_max_tokens: int = 4000
    # Databricks Foundation Model API: evaluation candidate, not deployed. DATABRICKS_HOST/DATABRICKS_TOKEN are
    # read directly from the environment (never the global ~/.databrickscfg).
    databricks_host: str | None = None
    databricks_endpoint: str = ""
    databricks_timeout_seconds: float = 30.0
    databricks_max_tokens: int = 4000
    databricks_structured_output: str | None = None  # `json_object` (default) | `prompt_only`

    @classmethod
    def from_env(cls) -> "Settings":
        as_of = os.getenv("ANALYSIS_AS_OF_DATE")
        origins = os.getenv("CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173")
        return cls(
            data_backend=os.getenv("DATA_BACKEND", "local"),
            package_dir=Path(os.getenv("DATA_PACKAGE_DIR", REPO_ROOT / "data" / "phase0_v1_0")),
            output_dir=Path(os.getenv("DATA_OUTPUT_DIR", REPO_ROOT / "data" / "output")),
            as_of_date=date.fromisoformat(as_of) if as_of else DEFAULT_AS_OF_DATE,
            cors_origins=tuple(o.strip() for o in origins.split(",") if o.strip()),
            explanation_mode=os.getenv("EXPLANATION_MODE", "auto"),
            explanation_model=os.getenv("EXPLANATION_MODEL", "claude-opus-5"),
            capture_rejected_explanations=os.getenv("CAPTURE_REJECTED_EXPLANATIONS", "true").lower() not in ("0", "false", "no", "off"),
            healthlake_datastore_id=os.getenv("HEALTHLAKE_DATASTORE_ID") or None,
            healthlake_region=os.getenv("HEALTHLAKE_REGION", "us-east-1"),
            healthlake_role_arn=os.getenv("HEALTHLAKE_ROLE_ARN") or None,
            healthlake_endpoint=os.getenv("HEALTHLAKE_ENDPOINT") or None,
            healthlake_write_outputs=os.getenv("HEALTHLAKE_WRITE_OUTPUTS", "false").lower() in ("1", "true", "yes", "on"),
            app_state_backend=os.getenv("APP_STATE_BACKEND", "local"),
            app_state_table=os.getenv("APP_STATE_TABLE", "medsafety-app-state"),
            explanation_provider=os.getenv("EXPLANATION_PROVIDER", "anthropic"),
            bedrock_region=os.getenv("BEDROCK_REGION", "us-east-1"),
            bedrock_timeout_seconds=float(os.getenv("BEDROCK_TIMEOUT_SECONDS", "20")),
            bedrock_max_tokens=int(os.getenv("BEDROCK_MAX_TOKENS", "4000")),
            bedrock_structured_output=os.getenv("BEDROCK_STRUCTURED_OUTPUT") or None,
            openai_timeout_seconds=float(os.getenv("OPENAI_TIMEOUT_SECONDS", "20")),
            openai_max_tokens=int(os.getenv("OPENAI_MAX_TOKENS", "4000")),
            openai_api_key_secret_arn=os.getenv("OPENAI_API_KEY_SECRET_ARN") or None,
            gemini_timeout_seconds=float(os.getenv("GEMINI_TIMEOUT_SECONDS", "20")),
            gemini_max_tokens=int(os.getenv("GEMINI_MAX_TOKENS", "4000")),
            databricks_host=os.getenv("DATABRICKS_HOST") or None,
            databricks_endpoint=os.getenv("DATABRICKS_ENDPOINT", ""),
            databricks_timeout_seconds=float(os.getenv("DATABRICKS_TIMEOUT_SECONDS", "30")),
            databricks_max_tokens=int(os.getenv("DATABRICKS_MAX_TOKENS", "4000")),
            databricks_structured_output=os.getenv("DATABRICKS_STRUCTURED_OUTPUT") or None,
        )
