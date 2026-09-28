from __future__ import annotations

from app.config import VALID_APP_STATE_BACKENDS, VALID_BACKENDS, Settings
from app.repository.base import ClinicalRepository
from app.repository.local import LocalFHIRRepository


def create_repository(settings: Settings) -> ClinicalRepository:
    backend = settings.data_backend
    if settings.app_state_backend not in VALID_APP_STATE_BACKENDS:
        raise ValueError(f"Unsupported APP_STATE_BACKEND: {settings.app_state_backend!r} (valid: {', '.join(VALID_APP_STATE_BACKENDS)})")
    if settings.app_state_backend == "dynamodb" and backend != "healthlake":
        raise ValueError("APP_STATE_BACKEND=dynamodb requires DATA_BACKEND=healthlake")

    if backend == "local":
        return LocalFHIRRepository(settings.package_dir, settings.output_dir)

    if backend == "healthlake":
        if not settings.healthlake_datastore_id:
            raise ValueError("HEALTHLAKE_DATASTORE_ID is required when DATA_BACKEND=healthlake")
        from app.repository.healthlake import HealthLakeFHIRRepository  # lazy: needs the optional `aws` extra

        return HealthLakeFHIRRepository.from_settings(settings)

    raise ValueError(f"Unsupported DATA_BACKEND: {backend!r} (valid: {', '.join(VALID_BACKENDS)})")
