"""SigV4-signed FHIR R4 REST client for one AWS HealthLake datastore.

    https://healthlake.<region>.amazonaws.com/datastore/<datastoreId>/r4/<Type>[/<id>][?query]

* Requests are signed with botocore's SigV4 (service ``healthlake``) using the default credential chain, or credentials from
  assuming ``role_arn`` (refreshed before expiry). Requires the optional ``aws`` extra (boto3/botocore + httpx).
* Retries with jittered exponential backoff on throttling (429) and 5xx / transport errors; honours ``Retry-After``.
* Paging only follows ``Bundle.link[next]`` URLs that stay inside this datastore's base URL (never signs a foreign URL).
* Logs one structured line per call: method, resource type, status, x-amzn-requestid, latency. Never request/response
  bodies, ids, query values or credentials.
* Writes send ``x-amzn-healthlake-fhir-validation-level: strict`` unless another level is chosen explicitly.
"""
from __future__ import annotations

import json
import logging
import random
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterator, Sequence

import httpx
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials

from app.redact import harden_sdk_logging

log = logging.getLogger(__name__)

FHIR_JSON = "application/fhir+json"
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
Params = Sequence[tuple[str, str]] | dict[str, str] | None


class HealthLakeError(RuntimeError):
    """Base error. The message never contains credentials or resource content."""

    def __init__(self, message: str, *, status: int | None = None, issues: list[str] | None = None,
                 request_id: str | None = None):
        super().__init__(message)
        self.status, self.issues, self.request_id = status, issues or [], request_id


class HealthLakeAuthError(HealthLakeError): ...  # no credentials / 401 / 403
class HealthLakeNotFound(HealthLakeError): ...  # 404 / 410
class HealthLakeConflict(HealthLakeError): ...  # 409 / 412
class HealthLakeValidationError(HealthLakeError): ...  # 400 / 422 (OperationOutcome issues in .issues)
class HealthLakeUnavailable(HealthLakeError): ...  # retries exhausted


@dataclass(frozen=True)
class HealthLakeResponse:
    status: int
    headers: httpx.Headers
    body: dict | None
    text: str

    @property
    def request_id(self) -> str | None:
        return self.headers.get("x-amzn-requestid")

    @property
    def etag(self) -> str | None:
        return self.headers.get("etag")

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def issues(self) -> list[str]:
        if isinstance(self.body, dict) and self.body.get("resourceType") == "OperationOutcome":
            return [i.get("diagnostics") or (i.get("details") or {}).get("text") or i.get("code", "") for i in self.body.get("issue", [])]
        return []

    def raise_for_status(self) -> "HealthLakeResponse":
        if self.ok:
            return self
        kwargs = dict(status=self.status, issues=self.issues(), request_id=self.request_id)
        summary = f"HealthLake returned HTTP {self.status}" + (f": {'; '.join(kwargs['issues'])[:200]}" if kwargs["issues"] else "")
        if self.status in (401, 403):
            raise HealthLakeAuthError(summary, **kwargs)
        if self.status in (404, 410):
            raise HealthLakeNotFound(summary, **kwargs)
        if self.status in (409, 412):
            raise HealthLakeConflict(summary, **kwargs)
        if self.status in (400, 422):
            raise HealthLakeValidationError(summary, **kwargs)
        raise HealthLakeError(summary, **kwargs)


class HealthLakeClient:
    def __init__(
        self,
        datastore_id: str,
        *,
        region: str = "us-east-1",
        endpoint: str | None = None,
        credentials: Callable[[], Credentials] | None = None,
        role_arn: str | None = None,
        session_name: str = "medsafety-app",
        boto_session=None,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
        max_retries: int = 4,
        backoff: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
        validation_level: str | None = "strict",
    ):
        harden_sdk_logging()  # httpx logs full URLs (query values) at INFO and botocore logs canonical requests at DEBUG
        self.datastore_id, self.region = datastore_id, region
        self.base_url = (endpoint or f"https://healthlake.{region}.amazonaws.com/datastore/{datastore_id}/r4").rstrip("/") + "/"
        self._credentials_fn, self._role_arn, self._session_name = credentials, role_arn, session_name
        self._boto_session = boto_session
        self._assumed: tuple[Credentials, float] | None = None
        self._http = httpx.Client(transport=transport, timeout=timeout)
        self._max_retries, self._backoff, self._sleep = max_retries, backoff, sleep
        self.validation_level = validation_level

    # ---- credentials -----------------------------------------------------------------------------------
    def _credentials(self) -> Credentials:
        if self._credentials_fn is not None:
            return self._credentials_fn()
        import boto3  # optional dependency

        session = self._boto_session or boto3.Session()
        if self._role_arn:
            now = time.time()
            if self._assumed is None or self._assumed[1] - now < 300:
                resp = session.client("sts", region_name=self.region).assume_role(
                    RoleArn=self._role_arn, RoleSessionName=self._session_name, DurationSeconds=3600)
                c = resp["Credentials"]
                expiry = c["Expiration"].astimezone(timezone.utc).timestamp() if isinstance(c["Expiration"], datetime) else now + 3600
                self._assumed = (Credentials(c["AccessKeyId"], c["SecretAccessKey"], c["SessionToken"]), expiry)
            return self._assumed[0]
        creds = session.get_credentials()
        if creds is None:
            raise HealthLakeAuthError("No AWS credentials found (configure a profile, environment credentials, or a role)")
        return creds.get_frozen_credentials()

    def _sign(self, method: str, url: str, body: bytes, headers: dict[str, str]) -> dict[str, str]:
        request = AWSRequest(method=method, url=url, data=body, headers=headers)
        SigV4Auth(self._credentials(), "healthlake", self.region).add_auth(request)
        return dict(request.headers.items())

    # ---- request plumbing ------------------------------------------------------------------------------
    @staticmethod
    def _canonical_query(url: str) -> str:
        """Re-encode an absolute URL's query the way SigV4 expects (every reserved character in a value percent-encoded).

        botocore signs the query string exactly as given, but HealthLake's `next` links carry raw `=`/`/`/`+` inside the page
        token; signed as-is, AWS computes a different canonical request and answers 403 SignatureDoesNotMatch.
        """
        base, sep, query = url.partition("?")
        if not sep:
            return url
        pairs = []
        for pair in query.split("&"):
            key, _, value = pair.partition("=")
            pairs.append(f"{urllib.parse.quote(urllib.parse.unquote(key), safe='')}={urllib.parse.quote(urllib.parse.unquote(value), safe='')}")
        return base + "?" + "&".join(pairs)

    def _url(self, path: str, params: Params) -> str:
        url = self._canonical_query(path) if path.startswith("http") else self.base_url + path.lstrip("/")
        if params:
            pairs = params.items() if isinstance(params, dict) else params
            query = "&".join(f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(str(v), safe='')}" for k, v in pairs)
            url += ("&" if "?" in url else "?") + query
        return url

    @staticmethod
    def _resource_type(url: str, base: str) -> str:
        rest = url[len(base):] if url.startswith(base) else url
        return rest.split("?", 1)[0].split("/", 1)[0] or "(system)"

    def request(self, method: str, path: str, *, params: Params = None, json_body: dict | None = None,
                headers: dict[str, str] | None = None) -> HealthLakeResponse:
        """Signed request; returns the response (any status) or raises HealthLakeUnavailable after retries."""
        url = self._url(path, params)
        if path.startswith("http") and not url.startswith(self.base_url):
            raise HealthLakeError("refusing to send a signed request outside this datastore's base URL")
        body = b"" if json_body is None else json.dumps(json_body, separators=(",", ":")).encode("utf-8")
        hdrs = {"Accept": FHIR_JSON, **(headers or {})}
        if body:
            hdrs["Content-Type"] = FHIR_JSON
        if method in ("POST", "PUT") and self.validation_level:
            hdrs.setdefault("x-amzn-healthlake-fhir-validation-level", self.validation_level)
        rtype = self._resource_type(url, self.base_url)

        last: str = "no attempt"
        for attempt in range(self._max_retries + 1):
            started = time.perf_counter()
            try:
                raw = self._http.request(method, url, content=body, headers=self._sign(method, url, body, hdrs))
            except httpx.TransportError as exc:
                last = type(exc).__name__
                log.warning("healthlake %s %s transport-error=%s attempt=%d", method, rtype, last, attempt + 1)
            else:
                elapsed = (time.perf_counter() - started) * 1000
                text = raw.text
                try:
                    parsed = json.loads(text) if text else None
                except json.JSONDecodeError:
                    parsed = None
                response = HealthLakeResponse(raw.status_code, raw.headers, parsed if isinstance(parsed, dict) else None, text)
                log.info("healthlake %s %s -> %d rid=%s %.0fms", method, rtype, response.status, response.request_id, elapsed)
                if response.status not in RETRYABLE_STATUS:
                    return response
                last = f"HTTP {response.status}"
                retry_after = response.headers.get("retry-after")
                if attempt < self._max_retries:
                    self._sleep(float(retry_after) if retry_after and retry_after.replace(".", "", 1).isdigit()
                                else self._delay(attempt))
                    continue
            if attempt < self._max_retries:
                self._sleep(self._delay(attempt))
        raise HealthLakeUnavailable(f"HealthLake unavailable after {self._max_retries + 1} attempts ({last})")

    def _delay(self, attempt: int) -> float:
        return self._backoff * (2 ** attempt) * (0.5 + random.random() / 2)

    # ---- FHIR helpers ----------------------------------------------------------------------------------
    def get_json(self, path: str, params: Params = None, headers: dict[str, str] | None = None) -> dict:
        resp = self.request("GET", path, params=params, headers=headers).raise_for_status()
        return resp.body or {}

    def read(self, resource_type: str, resource_id: str) -> dict:
        return self.get_json(f"{resource_type}/{urllib.parse.quote(resource_id, safe='')}")

    def search_entries(self, resource_type: str, params: Params = None, *, max_pages: int = 50,
                       headers: dict[str, str] | None = None) -> Iterator[dict]:
        """Yield every Bundle.entry of a search, following `next` links inside this datastore."""
        bundle = self.get_json(resource_type, params, headers)
        for _ in range(max_pages):
            yield from bundle.get("entry", [])
            nxt = next((l["url"] for l in bundle.get("link", []) if l.get("relation") == "next"), None)
            if not nxt:
                return
            bundle = self.get_json(nxt, None, headers)
        raise HealthLakeError(f"search exceeded {max_pages} pages; refusing to continue")

    def put(self, resource: dict, *, if_match: str | None = None, if_none_match: str | None = None,
            headers: dict[str, str] | None = None) -> HealthLakeResponse:
        h = dict(headers or {})
        if if_match:
            h["If-Match"] = if_match
        if if_none_match:
            h["If-None-Match"] = if_none_match
        return self.request("PUT", f"{resource['resourceType']}/{urllib.parse.quote(resource['id'], safe='')}",
                            json_body=resource, headers=h)
