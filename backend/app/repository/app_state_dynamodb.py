"""Application state in DynamoDB (Lambda deployment): saved analyses and rejected-explanation captures.

Same five methods as the local ``AppStateStore`` (duck-typed), so ``HealthLakeFHIRRepository`` does not care which one it gets.
Table ``medsafety-app-state``: partition ``pk`` (S), sort ``sk`` (S), TTL attribute ``ttl``, AWS-owned encryption at rest.

    pk=PATIENT#<id>   sk=ANALYSIS#<8-digit n>   doc=<analysis JSON string>, analysisId
    pk=PATIENT#<id>   sk=COUNTER                n=<last reserved number>   (atomic ADD)
    pk=CAPTURE#<analysisId>  sk=<timestamp>     doc=<redacted capture JSON>, ttl=<epoch seconds>

Analyses are stored as one JSON string (not native maps): DynamoDB rejects Python floats and the documents are opaque to it.
Only GetItem / PutItem / UpdateItem / Query are used (no Scan, no Delete) -- exactly what the Lambda role is allowed to do.
"""
from __future__ import annotations

import json
import re
import time

from app.repository.base import StoreUnavailable

CAPTURE_TTL_SECONDS = 30 * 24 * 3600
_ANALYSIS_ID = r"AN-{pid}-(\d{{3,}})"


class DynamoAppStateStore:
    def __init__(self, table: str, *, region: str = "us-east-1", client=None, clock=time.time):
        self._table, self._region, self._client, self._clock = table, region, client, clock

    def _dynamo(self):
        if self._client is None:
            import boto3  # bundled in the Lambda package; optional elsewhere

            self._client = boto3.client("dynamodb", region_name=self._region)
        return self._client

    def _call(self, op: str, **kwargs):
        try:
            return getattr(self._dynamo(), op)(TableName=self._table, **kwargs)
        except Exception as exc:  # noqa: BLE001 - botocore errors carry the service error code; never the item contents
            code = ((getattr(exc, "response", None) or {}).get("Error") or {}).get("Code")  # `response` may be None (timeouts)
            raise StoreUnavailable(f"application state store failed: {type(exc).__name__}{f' ({code})' if code else ''}") from exc

    @staticmethod
    def _sk(number: int) -> str:
        return f"ANALYSIS#{number:08d}"

    def save_analysis(self, patient_id: str, analysis_id: str, analysis: dict) -> None:
        match = re.fullmatch(_ANALYSIS_ID.format(pid=re.escape(patient_id)), analysis_id)
        if not match:
            raise ValueError("analysis id does not belong to this patient")
        self._call("put_item", Item={
            "pk": {"S": f"PATIENT#{patient_id}"}, "sk": {"S": self._sk(int(match.group(1)))},
            "analysisId": {"S": analysis_id}, "doc": {"S": json.dumps(analysis)},
        })

    def get_analysis(self, patient_id: str, analysis_id: str) -> dict | None:
        match = re.fullmatch(_ANALYSIS_ID.format(pid=re.escape(patient_id)), analysis_id)
        if not match:  # the id becomes part of a key: accept only this patient's own id shape
            return None
        item = self._call("get_item", Key={"pk": {"S": f"PATIENT#{patient_id}"}, "sk": {"S": self._sk(int(match.group(1)))}},
                          ConsistentRead=True).get("Item")
        return json.loads(item["doc"]["S"]) if item else None

    def get_latest_analysis(self, patient_id: str) -> dict | None:
        items = self._call(
            "query", KeyConditionExpression="pk = :pk AND begins_with(sk, :prefix)", ConsistentRead=True,
            ExpressionAttributeValues={":pk": {"S": f"PATIENT#{patient_id}"}, ":prefix": {"S": "ANALYSIS#"}},
            ScanIndexForward=False, Limit=1,
        ).get("Items", [])
        return json.loads(items[0]["doc"]["S"]) if items else None

    def next_analysis_number(self, patient_id: str) -> int:
        """Reserves and returns the next number (atomic counter): unique per patient even for concurrent analyses."""
        resp = self._call("update_item", Key={"pk": {"S": f"PATIENT#{patient_id}"}, "sk": {"S": "COUNTER"}},
                          UpdateExpression="ADD #n :one", ExpressionAttributeNames={"#n": "n"},
                          ExpressionAttributeValues={":one": {"N": "1"}}, ReturnValues="UPDATED_NEW")
        return int(resp["Attributes"]["n"]["N"])

    def save_rejected_explanation(self, record: dict) -> None:
        stamp = re.sub(r"[^0-9A-Za-z]", "", record["capturedAt"])
        self._call("put_item", Item={
            "pk": {"S": f"CAPTURE#{record['analysisId']}"}, "sk": {"S": stamp},
            "doc": {"S": json.dumps(record)}, "ttl": {"N": str(int(self._clock()) + CAPTURE_TTL_SECONDS)},
        })

    def ping(self) -> None:
        """A GetItem on a reserved key that is never written -- proves the table, region and credentials all work.
        Deliberately NOT DescribeTable: the deployed Lambda role only has Get/Put/Update/Query (least privilege), and
        readiness must not need a new IAM permission it does not otherwise use."""
        self._call("get_item", Key={"pk": {"S": "PING#READINESS"}, "sk": {"S": "PING"}})
