"""DynamoAppStateStore against the in-memory DynamoDB fake."""
from __future__ import annotations

import json
import threading

import pytest

from app.repository.app_state_dynamodb import CAPTURE_TTL_SECONDS, DynamoAppStateStore
from app.repository.base import StoreUnavailable
from tests.support.fake_dynamodb import TABLE, FakeDynamoDB


@pytest.fixture()
def fake():
    return FakeDynamoDB()


@pytest.fixture()
def store(fake):
    return DynamoAppStateStore(TABLE, client=fake, clock=lambda: 1_000_000.0)


ANALYSIS = {"analysisId": "AN-P001-001", "overallSeverity": "HIGH", "findings": [{"value": 5.8, "unit": "mmol/L"}], "note": "µg – ü"}


def test_analysis_round_trips_exactly_including_floats_and_unicode(store):
    store.save_analysis("P001", "AN-P001-001", ANALYSIS)
    assert store.get_analysis("P001", "AN-P001-001") == ANALYSIS


def test_unknown_or_foreign_ids_return_none_without_touching_the_table(store, fake):
    store.save_analysis("P001", "AN-P001-001", ANALYSIS)
    fake.calls.clear()
    assert store.get_analysis("P001", "AN-P001-002") is None
    assert store.get_analysis("P001", "AN-P002-001") is None  # another patient's id
    assert store.get_analysis("P001", "../../etc") is None and store.get_analysis("P001", "AN-P001-1") is None
    assert fake.calls == ["get_item"]  # only the well-formed id reached the table


def test_saving_an_id_that_belongs_to_another_patient_is_refused(store):
    with pytest.raises(ValueError):
        store.save_analysis("P001", "AN-P002-001", ANALYSIS)


def test_latest_is_by_number_not_by_string_order_past_999(store):
    for n in (1, 2, 999, 1000):
        store.save_analysis("P001", f"AN-P001-{n:03d}", {"analysisId": f"AN-P001-{n:03d}"})
    assert store.get_latest_analysis("P001")["analysisId"] == "AN-P001-1000"
    assert store.get_latest_analysis("P009") is None


def test_numbers_are_unique_and_gapless_under_concurrency(store):
    got, lock = [], threading.Lock()

    def worker():
        n = store.next_analysis_number("P001")
        with lock:
            got.append(n)

    threads = [threading.Thread(target=worker) for _ in range(40)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(got) == list(range(1, 41))
    assert store.next_analysis_number("P002") == 1  # counters are per patient


def test_overwriting_an_analysis_keeps_one_item_per_analysis(store, fake):
    store.save_analysis("P001", "AN-P001-001", {"v": 1})
    store.save_analysis("P001", "AN-P001-001", {"v": 2, "aiExplanation": {"mode": "llm"}})
    assert len([k for k in fake.items if k[1].startswith("ANALYSIS#")]) == 1
    assert store.get_analysis("P001", "AN-P001-001")["v"] == 2


def test_rejected_capture_has_a_thirty_day_ttl_and_its_own_key_space(store, fake):
    store.save_rejected_explanation({"capturedAt": "2026-09-19T11:00:00.123456+00:00", "analysisId": "AN-P001-001", "rawOutput": "x"})
    (key,) = [k for k in fake.items if k[0].startswith("CAPTURE#")]
    assert key == ("CAPTURE#AN-P001-001", "20260919T1100001234560000")
    assert int(fake.items[key]["ttl"]["N"]) == 1_000_000 + CAPTURE_TTL_SECONDS
    assert json.loads(fake.items[key]["doc"]["S"])["rawOutput"] == "x"


def test_only_get_put_update_query_are_ever_used(store, fake):
    store.save_analysis("P001", "AN-P001-001", ANALYSIS)
    store.get_analysis("P001", "AN-P001-001")
    store.get_latest_analysis("P001")
    store.next_analysis_number("P001")
    store.save_rejected_explanation({"capturedAt": "2026-09-19T11:00:00+00:00", "analysisId": "AN-P001-001"})
    assert set(fake.calls) == {"put_item", "get_item", "query", "update_item"}  # never scan / delete


def test_store_failures_become_store_unavailable_without_leaking_item_content(store, fake):
    fake.fail_with = "ProvisionedThroughputExceededException"
    with pytest.raises(StoreUnavailable, match="ProvisionedThroughputExceededException") as exc:
        store.save_analysis("P001", "AN-P001-001", {"secret": "SECRET-CLINICAL-TEXT"})
    assert "SECRET-CLINICAL-TEXT" not in str(exc.value)


def test_reads_are_strongly_consistent_so_explain_sees_the_analysis_just_saved():
    calls = {}

    class Spy(FakeDynamoDB):
        def get_item(self, **kw):
            calls["get_item"] = kw.get("ConsistentRead")
            return super().get_item(**kw)

        def query(self, **kw):
            calls["query"] = kw.get("ConsistentRead")
            return super().query(**kw)

    s = DynamoAppStateStore(TABLE, client=Spy())
    s.save_analysis("P001", "AN-P001-001", {"a": 1})
    s.get_analysis("P001", "AN-P001-001")
    s.get_latest_analysis("P001")
    assert calls == {"get_item": True, "query": True}


def test_a_timeout_style_error_without_a_response_still_becomes_store_unavailable(store, fake):
    from botocore.exceptions import ReadTimeoutError

    class Slow(FakeDynamoDB):
        def get_item(self, **kw):
            raise ReadTimeoutError(endpoint_url="https://dynamodb.us-east-1.amazonaws.com")

    with pytest.raises(StoreUnavailable, match="ReadTimeoutError"):
        DynamoAppStateStore(TABLE, client=Slow()).get_analysis("P001", "AN-P001-001")
