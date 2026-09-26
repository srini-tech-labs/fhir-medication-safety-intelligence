"""The V0-V19 verifier run end-to-end against the FakeHealthLake (offline), plus checks that it detects real problems."""
from __future__ import annotations

import importlib.util
import json

import httpx
import pytest

from app.repository.healthlake_client import HealthLakeClient
from app.config import REPO_ROOT
from tests.support.fake_healthlake import APP_KEY, DATASTORE_ID, VERIFIER_KEY, FakeHealthLake, credentials

spec = importlib.util.spec_from_file_location("healthlake_verify", REPO_ROOT / "scripts" / "healthlake_verify.py")
hv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hv)


def make_suite(fake: FakeHealthLake):
    client = lambda key: HealthLakeClient(DATASTORE_ID, credentials=credentials(key), transport=fake.transport(), sleep=lambda s: None)
    raw = httpx.Client(transport=fake.transport())
    return hv.Suite(client(VERIFIER_KEY), client(APP_KEY), raw.get, poll_seconds=5, history_poll_seconds=2, sleep=lambda s: None)


@pytest.fixture()
def fake():
    return FakeHealthLake(page_size=3)


def test_all_checks_pass_and_the_datastore_is_left_exactly_as_imported(fake):
    report = make_suite(fake).run_all()
    assert report["failed"] == [], [r for r in report["results"] if r["status"] != "PASS"]
    assert [r["id"] for r in report["results"]] == [f"V{n}" for n in range(20)]
    # every temporary resource was deleted (soft delete keeps history) and all 53 frozen resources are still version 1
    frozen = hv.frozen_resources()
    for rtype, ids in fake.store.items():
        for rid, versions in ids.items():
            if (rtype, rid) in frozen:
                assert len(versions) == 1 and not versions[-1]["deleted"], (rtype, rid)
            else:  # server-assigned ids (POST) and zz-phase3-tmp-* ids: all deleted
                assert versions[-1]["deleted"], (rtype, rid)
    assert sum(1 for t in hv.EXPECTED_COUNTS for r in fake.all_current(t)) == 53
    assert all(r["requests"] for r in report["results"])  # request ids recorded per check


def test_frozen_data_is_only_read_never_written(fake):
    make_suite(fake).run_all()
    frozen_ids = {rid for (_, rid) in hv.frozen_resources()}
    writes = [r for r in fake.requests if r["method"] in ("PUT", "DELETE") and r["path"].split("/")[-1] in frozen_ids]
    assert writes == []


def test_report_contains_no_clinical_content(fake):
    text = json.dumps(make_suite(fake).run_all())
    for needle in ("Lisa", "Potassium was", "Progress Note", "Lisinopril"):
        assert needle not in text


def test_app_role_is_used_only_for_the_authorization_check(fake):
    make_suite(fake).run_all()
    app_calls = [r for r in fake.requests if r["key"] == APP_KEY]
    assert app_calls and {r["action"] for r in app_calls} <= {"ReadResource", "DeleteResource", "GetHistoryByResourceId", "ProcessBundle", "UpdateResource"}
    assert [r["action"] for r in app_calls].count("DeleteResource") == 1  # denied with 403; nothing was actually deleted by it


def test_a_frozen_resource_being_modified_is_detected(fake):
    fake._commit(dict(fake.current("Observation", "obs-p001-01"), valueString="tampered"))  # version 2
    report = make_suite(fake).run_all()
    assert "V19" in report["failed"]
    (v19,) = [r for r in report["results"] if r["id"] == "V19"]
    assert "obs-p001-01" in v19["detail"]["reason"]


def test_missing_import_is_detected(fake):
    del fake.store["Observation"]["obs-p001-01"]
    report = make_suite(fake).run_all()
    assert {"V3", "V4"} <= set(report["failed"])


def test_lenient_validation_would_be_detected(fake, monkeypatch):
    """If the server accepted an Observation without `status` (i.e. validation weakened), V17 must fail."""
    monkeypatch.setattr(type(fake), "_validate", lambda self, req, rtype, body, rid=None: None)
    report = make_suite(fake).run_all()
    assert "V17" in report["failed"]


def test_over_permissive_app_role_would_be_detected(fake):
    from tests.support import fake_healthlake as fh

    fh.PERMISSIONS[fh.APP_KEY] = fh.PERMISSIONS[fh.APP_KEY] | {"DeleteResource"}
    try:
        report = make_suite(fake).run_all()
    finally:
        fh.PERMISSIONS[fh.APP_KEY] = fh.APP_ACTIONS
    assert "V18" in report["failed"]


def test_non_atomic_transactions_would_be_detected(fake, monkeypatch):
    original = type(fake)._bundle_request

    def sloppy(self, req):
        body = json.loads(req.content)
        if body["type"] == "transaction":
            body["type"] = "batch"  # commits the valid entries even though another entry is invalid
            req = httpx.Request(req.method, req.url, headers={k: v for k, v in req.headers.items() if k != "content-length"}, json=body)
        return original(self, req)

    monkeypatch.setattr(type(fake), "_bundle_request", sloppy)
    assert "V16" in make_suite(fake).run_all()["failed"]


def test_polling_has_a_deadline_instead_of_hanging(fake):
    suite = make_suite(fake)
    suite.poll_seconds = 0
    with pytest.raises(hv.Fail, match="not visible"):
        suite.eventually(lambda: False)


def test_eventually_consistent_history_is_polled_not_assumed(fake):
    """Live HealthLake returned empty history for fresh writes/imports for minutes; the verifier must wait, not fail."""
    fake.history_empty_reads = 5
    suite = make_suite(fake)
    suite.history_poll_seconds = 60
    report = suite.run_all()
    assert report["failed"] == [], [r for r in report["results"] if r["status"] != "PASS"]


def test_history_that_never_appears_fails_with_the_lag_deadline(fake):
    fake.history_empty_reads = 10**6
    report = make_suite(fake).run_all()
    assert {"V13", "V19"} <= set(report["failed"])


def test_post_has_no_location_header_and_duplicate_key_reports_original_via_location(fake):
    report = make_suite(fake).run_all()
    by_id = {r["id"]: r for r in report["results"]}
    assert by_id["V11"]["detail"]["location_header_present"] is False
    assert by_id["V14"]["detail"]["original_in_location_header"] is True


def test_paging_signs_next_links_whose_tokens_contain_raw_reserved_characters(fake):
    """Regression for the live V9 failure: next links end in `==`; botocore signs the query as given, AWS canonicalises it."""
    report = make_suite(fake).run_all()
    assert "V9" not in report["failed"]
    (v9,) = [r for r in report["results"] if r["id"] == "V9"]
    assert sum(1 for q in v9["requests"] if q["status"] == 200) >= 4


def test_two_runs_against_the_same_datastore_do_not_collide_on_deleted_ids(fake):
    """Live: a second run failed because `zz-phase3-tmp-obs-put` was a tombstone (If-None-Match: * -> 412, GET -> 410)."""
    assert make_suite(fake).run_all()["failed"] == []
    assert make_suite(fake).run_all()["failed"] == []
    ids = {rid for rid, vs in fake.store["Observation"].items() if rid.startswith("zz-phase3-tmp-")}
    assert len({i.split("-obs-")[0] for i in ids}) >= 2  # distinct per-run prefixes


def test_final_totals_are_polled_because_deletes_show_up_late(fake):
    """Live: Patient total was 11 immediately after the temp patient was deleted, 10 shortly after."""
    fake.total_stale_reads = 3
    suite = make_suite(fake)
    assert suite.run_all()["failed"] == []


# ---- --checks subset (shortened post-recreation verification) --------------------------------------------------

def test_checks_subset_runs_only_the_requested_ids_plus_v19(fake):
    report = make_suite(fake).run_all("V0,V1,V2,V3,V4,V5")
    assert [r["id"] for r in report["results"]] == ["V0", "V1", "V2", "V3", "V4", "V5", "V19"]
    assert report["failed"] == []
    assert report["checks"] == "V0,V1,V2,V3,V4,V5"


def test_checks_subset_is_order_and_case_insensitive_and_ignores_duplicates(fake):
    report = make_suite(fake).run_all("v3, V1,v1")
    assert [r["id"] for r in report["results"]] == ["V1", "V3", "V19"]  # plan order, not requested order; no repeats


def test_checks_v19_always_runs_even_if_not_requested():
    fake = FakeHealthLake(page_size=3)
    report = make_suite(fake).run_all("V0")
    assert "V19" in [r["id"] for r in report["results"]]


def test_checks_unknown_id_is_rejected_not_silently_ignored(fake):
    with pytest.raises(ValueError, match="V99"):
        make_suite(fake).run_all("V0,V99")


def test_checks_default_none_still_runs_the_full_v0_to_v19_plan(fake):
    report = make_suite(fake).run_all()
    assert [r["id"] for r in report["results"]][0] == "V0"
    assert [r["id"] for r in report["results"]][-1] == "V19"
    assert len(report["results"]) == 20
    assert report["checks"] == "all"
