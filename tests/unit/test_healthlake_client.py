"""HealthLakeClient against the in-memory FakeHealthLake (offline; the fake verifies real SigV4 signatures)."""
from __future__ import annotations

import logging

import httpx
import pytest

from app.repository.healthlake_client import (
    HealthLakeAuthError, HealthLakeClient, HealthLakeConflict, HealthLakeError, HealthLakeNotFound,
    HealthLakeUnavailable, HealthLakeValidationError,
)
from tests.support.fake_healthlake import APP_KEY, BASE, DATASTORE_ID, VERIFIER_KEY, FakeHealthLake, credentials

SLEEPS: list[float] = []


def make(fake: FakeHealthLake, key: str = VERIFIER_KEY, **kw) -> HealthLakeClient:
    SLEEPS.clear()
    kw.setdefault("sleep", SLEEPS.append)
    return HealthLakeClient(DATASTORE_ID, credentials=credentials(key), transport=fake.transport(), **kw)


@pytest.fixture()
def fake():
    return FakeHealthLake()


def obs(rid: str, **extra) -> dict:
    return {"resourceType": "Observation", "id": rid, "status": "final", "code": {"text": "temp"},
            "subject": {"reference": "Patient/patient-p001"}, **extra}


# ---- authentication ------------------------------------------------------------------------------------
def test_requests_are_sigv4_signed_and_accepted(fake):
    assert make(fake).read("Patient", "patient-p001")["identifier"][0]["value"] == "P001"
    assert fake.requests[-1]["headers"]["authorization"].startswith("AWS4-HMAC-SHA256 Credential=" + VERIFIER_KEY)


def test_unsigned_and_forged_requests_are_403(fake):
    raw = httpx.Client(transport=fake.transport())
    assert raw.get(BASE + "Patient/patient-p001").status_code == 403
    forged = HealthLakeClient(DATASTORE_ID, transport=fake.transport(),
                              credentials=lambda: __import__("botocore.credentials", fromlist=["Credentials"]).Credentials(VERIFIER_KEY, "wrong-secret"))
    with pytest.raises(HealthLakeAuthError):
        forged.read("Patient", "patient-p001")


def test_no_credentials_is_a_clear_auth_error():
    class NoCreds:
        def get_credentials(self):
            return None

    client = HealthLakeClient(DATASTORE_ID, boto_session=NoCreds(), transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    with pytest.raises(HealthLakeAuthError, match="No AWS credentials"):
        client.read("Patient", "x")


def test_assume_role_credentials_are_cached_and_refreshed():
    from datetime import datetime, timedelta, timezone

    calls = []

    class Sts:
        def __init__(self, ttl):
            self.ttl = ttl

        def assume_role(self, **kw):
            calls.append(kw)
            return {"Credentials": {"AccessKeyId": APP_KEY, "SecretAccessKey": "app-secret-test-value", "SessionToken": "tok",
                                    "Expiration": datetime.now(timezone.utc) + timedelta(seconds=self.ttl)}}

    class Session:
        ttl = 3600

        def client(self, name, region_name=None):
            assert name == "sts"
            return Sts(self.ttl)

    session = Session()
    client = HealthLakeClient(DATASTORE_ID, role_arn="arn:aws:iam::123456789012:role/MedSafetyHealthLakeAppRole",
                              boto_session=session, transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    client._credentials(); client._credentials()
    assert len(calls) == 1 and calls[0]["RoleArn"].endswith("MedSafetyHealthLakeAppRole") and calls[0]["RoleSessionName"] == "medsafety-app"
    client._assumed = (client._assumed[0], client._assumed[1] - 3500)  # about to expire -> refresh
    client._credentials()
    assert len(calls) == 2


# ---- reads and errors -----------------------------------------------------------------------------------
def test_read_and_error_mapping(fake):
    c = make(fake)
    with pytest.raises(HealthLakeNotFound):
        c.read("Patient", "P001")  # the application id is only an identifier, not the FHIR id
    resp = c.request("GET", "Patient/patient-p001")
    assert resp.status == 200 and resp.etag == 'W/"1"' and resp.request_id


def test_permission_denied_maps_to_auth_error(fake):
    app = make(fake, APP_KEY)
    with pytest.raises(HealthLakeAuthError):
        app.request("DELETE", "Observation/obs-p001-01").raise_for_status()
    assert fake.current("Observation", "obs-p001-01")  # untouched


# ---- retries ---------------------------------------------------------------------------------------------
def test_retries_on_5xx_and_429_then_succeeds(fake):
    c = make(fake)
    fake.fail_next = [503, 429, 500]
    assert c.read("Patient", "patient-p001")["id"] == "patient-p001"
    assert len(SLEEPS) == 3 and SLEEPS[1] == 0.0  # Retry-After: 0 honoured for the 429


def test_retries_exhausted_raises_unavailable(fake):
    c = make(fake, max_retries=2)
    fake.fail_next = [503, 503, 503, 503]
    with pytest.raises(HealthLakeUnavailable):
        c.read("Patient", "patient-p001")
    assert len(SLEEPS) == 2


def test_transport_errors_are_retried():
    attempts = []

    def handler(request):
        attempts.append(1)
        raise httpx.ConnectError("boom")

    client = HealthLakeClient(DATASTORE_ID, credentials=credentials(APP_KEY), transport=httpx.MockTransport(handler),
                              max_retries=2, sleep=lambda s: None)
    with pytest.raises(HealthLakeUnavailable):
        client.read("Patient", "x")
    assert len(attempts) == 3


def test_4xx_is_not_retried(fake):
    c = make(fake)
    with pytest.raises(HealthLakeNotFound):
        c.read("Patient", "nope")
    assert SLEEPS == []


# ---- paging ------------------------------------------------------------------------------------------------
def test_search_follows_next_links_across_pages(fake):
    c = make(fake)
    entries = list(c.search_entries("Patient", [("_count", "3"), ("_sort", "_id")]))
    assert [e["resource"]["id"] for e in entries] == [f"patient-p{n:03d}" for n in range(1, 11)]
    assert sum(1 for r in fake.requests if r["path"] == "Patient") == 4


def test_paging_never_signs_a_foreign_url(fake):
    c = make(fake)
    with pytest.raises(HealthLakeError, match="outside this datastore"):
        c.get_json("https://evil.example.com/datastore/x/r4/Patient?_getpages=1")
    assert fake.requests == []


def test_page_limit_guard(fake):
    c = make(fake)
    fake.page_size = 1
    with pytest.raises(HealthLakeError, match="exceeded 2 pages"):
        list(c.search_entries("Patient", [("_count", "1")], max_pages=2))


def test_query_values_are_percent_encoded_and_pairs_preserved(fake):
    c = make(fake)
    ident = "https://example.org/synthetic-patient-id|P001"
    assert len(list(c.search_entries("Patient", [("identifier", ident)]))) == 1
    q = fake.requests[-1]["query"]
    assert "identifier=https%3A%2F%2Fexample.org%2Fsynthetic-patient-id%7CP001" in q
    pairs = [("_revinclude", "Observation:patient"), ("_revinclude", "Encounter:patient")]
    list(c.search_entries("Patient", [("_id", "patient-p001"), *pairs]))
    assert fake.requests[-1]["query"].count("_revinclude=") == 2


# ---- writes -----------------------------------------------------------------------------------------------
def test_put_uses_strict_validation_header_and_versions(fake):
    c = make(fake)
    r1 = c.put(obs("zz-phase3-tmp-a")).raise_for_status()
    assert r1.status == 201 and r1.etag == 'W/"1"'
    assert fake.requests[-1]["headers"]["x-amzn-healthlake-fhir-validation-level"] == "strict"
    r2 = c.put(obs("zz-phase3-tmp-a", status="amended"), if_match=r1.etag).raise_for_status()
    assert r2.status == 200 and r2.etag == 'W/"2"'


def test_stale_if_match_and_if_none_match_conflict(fake):
    c = make(fake)
    c.put(obs("zz-phase3-tmp-b")).raise_for_status()
    with pytest.raises(HealthLakeConflict):
        c.put(obs("zz-phase3-tmp-b"), if_match='W/"9"').raise_for_status()
    with pytest.raises(HealthLakeConflict):
        c.put(obs("zz-phase3-tmp-b"), if_none_match="*").raise_for_status()
    assert fake.current("Observation", "zz-phase3-tmp-b")["meta"]["versionId"] == "1"


def test_invalid_resource_is_rejected_with_operation_outcome_issues(fake):
    c = make(fake)
    bad = obs("zz-phase3-tmp-c")
    del bad["status"]
    with pytest.raises(HealthLakeValidationError) as exc:
        c.put(bad).raise_for_status()
    assert exc.value.status == 400 and "status" in " ".join(exc.value.issues)
    assert fake.current("Observation", "zz-phase3-tmp-c") is None


def test_validation_level_is_configurable_but_defaults_to_strict(fake):
    assert HealthLakeClient(DATASTORE_ID, credentials=credentials(APP_KEY)).validation_level == "strict"


# ---- logging -----------------------------------------------------------------------------------------------
FORBIDDEN = ("P001", "zz-phase3-tmp-log", "SECRET-CLINICAL-VALUE", "example.org", "wrong-secret", "verifier-secret-test-value",
             VERIFIER_KEY, "Signature=")


@pytest.fixture()
def restore_logging_levels():
    from app.redact import _SDK_LOGGERS

    saved = {n: logging.getLogger(n).level for n in _SDK_LOGGERS}
    factory = logging.getLogRecordFactory()
    logging.setLogRecordFactory(logging.LogRecord)  # stock factory: the app-wide redaction backstop must not mask what we probe
    yield
    logging.setLogRecordFactory(factory)
    for name, level in saved.items():
        logging.getLogger(name).setLevel(level)


def _exercise(fake, caplog, *, harden: bool) -> str:
    caplog.set_level(logging.DEBUG)  # everything at DEBUG, like `LOG_LEVEL=DEBUG`
    c = make(fake)
    if not harden:  # emulate an un-hardened process: undo what the client constructor pinned
        for name in ("httpx", "botocore"):
            logging.getLogger(name).setLevel(logging.NOTSET)
    list(c.search_entries("Patient", [("identifier", "https://example.org/synthetic-patient-id|P001"), ("_count", "1")]))
    c.put(obs("zz-phase3-tmp-log", valueString="SECRET-CLINICAL-VALUE"))
    return "\n".join(r.getMessage() for r in caplog.records)


def test_probe_is_sensitive_unhardened_httpx_and_botocore_do_leak(fake, caplog, restore_logging_levels):
    """Guards against a vacuous pass: without the pin, the same calls DO put the identifier and signatures in the logs."""
    text = _exercise(fake, caplog, harden=False)
    assert "P001" in text and "Signature" in text


def test_logs_contain_no_bodies_ids_query_values_or_credentials(fake, caplog, restore_logging_levels):
    text = _exercise(fake, caplog, harden=True)
    assert "healthlake GET Patient -> 200" in text and "healthlake PUT Observation -> 201" in text
    for forbidden in FORBIDDEN:
        assert forbidden not in text, forbidden


def test_next_link_with_raw_reserved_characters_is_canonicalised_before_signing(fake):
    """AWS canonicalises query values (`=`, `/`, `+` percent-encoded); signing the raw next link gave 403 SignatureDoesNotMatch live."""
    c = make(fake)
    first = c.get_json("Patient", [("_count", "3"), ("_sort", "_id")])
    nxt = next(l["url"] for l in first["link"] if l["relation"] == "next")
    assert "==" in nxt  # the fake, like HealthLake, hands out raw tokens
    assert c.request("GET", nxt).status == 200
    assert HealthLakeClient._canonical_query("https://h/x?a=b==&c=d/e+f&g=%41") == "https://h/x?a=b%3D%3D&c=d%2Fe%2Bf&g=A"
