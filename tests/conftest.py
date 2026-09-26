from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import REPO_ROOT, Settings
from app.container import Container, build_container
from app.services.explanation.mock import MockExplanationService

PACKAGE_DIR = REPO_ROOT / "data" / "phase0_v1_0"


@pytest.fixture(scope="session")
def package_dir() -> Path:
    return PACKAGE_DIR


@pytest.fixture()
def settings(tmp_path) -> Settings:
    """Local backend, isolated output dir, pinned as-of date, no API key needed."""
    base = Settings.from_env()
    return Settings(
        data_backend="local", package_dir=PACKAGE_DIR, output_dir=tmp_path / "output",
        as_of_date=base.as_of_date, cors_origins=(), explanation_mode="mock",
        explanation_model=base.explanation_model,
    )


@pytest.fixture()
def container(settings) -> Container:
    return build_container(settings, explainer=MockExplanationService())


def load_json(path: Path) -> dict:
    return json.loads(path.read_text("utf-8"))
