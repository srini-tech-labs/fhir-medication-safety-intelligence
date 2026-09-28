"""LIVE parity against a real AWS HealthLake datastore. Opt-in; never runs in `make test`.

    RUN_HEALTHLAKE_LIVE=1 HEALTHLAKE_DATASTORE_ID=<id> HEALTHLAKE_ROLE_ARN=<App role ARN> \
        AWS_PROFILE=<profile> backend/.venv/bin/python -m pytest -m live_aws tests/live_aws -o addopts="-q"

Uses the App role only (read/search/create/update), i.e. exactly what the application will have at runtime. Read-only: the
repository is built with `write_fhir_outputs=False` (the default), so nothing is written to the datastore.
"""
from __future__ import annotations

import os

import pytest

from app.config import Settings
from app.container import build_container
from app.repository.healthlake import HealthLakeFHIRRepository
from app.services.explanation.mock import MockExplanationService
from tests.conftest import PACKAGE_DIR

pytestmark = [
    pytest.mark.live_aws,
    pytest.mark.skipif(os.getenv("RUN_HEALTHLAKE_LIVE") != "1" or not os.getenv("HEALTHLAKE_DATASTORE_ID"),
                       reason="set RUN_HEALTHLAKE_LIVE=1 and HEALTHLAKE_DATASTORE_ID to run against real AWS"),
]
PATIENTS = [f"P{n:03d}" for n in range(1, 11)]


@pytest.fixture(scope="module")
def live_container(tmp_path_factory):
    base = Settings.from_env()
    cfg = Settings(**{**base.__dict__, "data_backend": "healthlake", "package_dir": PACKAGE_DIR,
                      "output_dir": tmp_path_factory.mktemp("hl-live-output"), "explanation_mode": "mock",
                      "healthlake_write_outputs": False})
    repo = HealthLakeFHIRRepository.from_settings(cfg)
    return build_container(cfg, repository=repo, explainer=MockExplanationService())


@pytest.fixture(scope="module")
def local_container(tmp_path_factory):
    base = Settings.from_env()
    cfg = Settings(**{**base.__dict__, "data_backend": "local", "package_dir": PACKAGE_DIR,
                      "output_dir": tmp_path_factory.mktemp("local-output"), "explanation_mode": "mock"})
    return build_container(cfg, explainer=MockExplanationService())


def dump(model):
    return model.model_dump(mode="json", by_alias=True)


def test_patient_list_matches_local(live_container, local_container):
    assert live_container.repository.get_patients() == local_container.repository.get_patients()


@pytest.mark.parametrize("pid", PATIENTS)
def test_snapshot_and_deterministic_analysis_match_local(live_container, local_container, pid):
    assert dump(live_container.snapshots.snapshot(pid)) == dump(local_container.snapshots.snapshot(pid))
    a_live, a_local = dump(live_container.analyses.analyze(pid)), dump(local_container.analyses.analyze(pid))
    for volatile in ("generatedAt",):
        a_live.pop(volatile), a_local.pop(volatile)
    assert a_live == a_local


def test_golden_outcomes_from_healthlake(live_container):
    run = live_container.analyses.analyze
    assert run("P001").overall_severity == "HIGH"
    p6 = run("P006")
    assert [f.rule_id for f in p6.findings] == ["DDI-003"] and [g.rule_id for g in p6.data_gaps] == ["DG-001"]
    assert [(f.rule_id, f.severity) for f in run("P008").findings] == [("DL-001", "HIGH"), ("DDI-003", "MODERATE")]
    p9, p10 = run("P009"), run("P010")
    assert (p9.overall_severity, p9.findings, p9.data_gaps) == ("NONE", [], [])
    assert p10.overall_severity == "NEEDS_DATA" and live_container.snapshots.snapshot("P010").labs == []


def test_analysis_wrote_no_derived_resources_to_the_datastore(live_container):
    client = live_container.repository._client
    for rtype in ("DetectedIssue", "RiskAssessment"):
        assert client.get_json(rtype, [("_total", "accurate"), ("_count", "1")])["total"] == 0
