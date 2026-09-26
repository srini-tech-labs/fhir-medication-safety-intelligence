"""Composition root: wires settings -> repository -> services. Tests build their own Container."""
from __future__ import annotations

from dataclasses import dataclass

from app.config import Settings
from app.repository.base import ClinicalRepository
from app.repository.factory import create_repository
from app.rules.catalog import RuleCatalog
from app.services.analysis_service import AnalysisService
from app.services.explanation.base import ExplanationService
from app.services.explanation.factory import create_explanation_service
from app.services.snapshot_service import SnapshotService


@dataclass(frozen=True)
class Container:
    settings: Settings
    repository: ClinicalRepository
    snapshots: SnapshotService
    analyses: AnalysisService
    explainer: ExplanationService


def build_container(
    settings: Settings | None = None,
    repository: ClinicalRepository | None = None,
    explainer: ExplanationService | None = None,
) -> Container:
    settings = settings or Settings.from_env()
    repository = repository or create_repository(settings)
    explainer = explainer or create_explanation_service(settings, repository)
    catalog = RuleCatalog.load(settings.package_dir)
    snapshots = SnapshotService(repository, settings.as_of_date)
    analyses = AnalysisService(repository, catalog, explainer, snapshots, settings.as_of_date)
    return Container(settings, repository, snapshots, analyses, explainer)
