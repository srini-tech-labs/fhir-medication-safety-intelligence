"""In-memory stand-in for the boto3 DynamoDB *client* surface the app uses: put_item, get_item, update_item (ADD counter), query.

Records every call in ``calls`` so tests can prove the app never scans or deletes. Thread-safe (atomic counter tests)."""
from __future__ import annotations

import threading

from botocore.exceptions import ClientError

TABLE = "medsafety-app-state"


class FakeDynamoDB:
    def __init__(self, table: str = TABLE):
        self.table, self.items, self.calls = table, {}, []
        self.fail_with: str | None = None  # error code to raise on the next call
        self._lock = threading.Lock()

    def _enter(self, op: str, TableName: str) -> None:
        self.calls.append(op)
        assert TableName == self.table, f"unexpected table {TableName}"
        if self.fail_with:
            code, self.fail_with = self.fail_with, None
            raise ClientError({"Error": {"Code": code, "Message": "injected"}}, op)

    @staticmethod
    def _key(item: dict) -> tuple[str, str]:
        return item["pk"]["S"], item["sk"]["S"]

    def put_item(self, TableName, Item, **_):
        with self._lock:
            self._enter("put_item", TableName)
            self.items[self._key(Item)] = dict(Item)
        return {}

    def get_item(self, TableName, Key, **_):
        with self._lock:
            self._enter("get_item", TableName)
            item = self.items.get(self._key(Key))
        return {"Item": dict(item)} if item else {}

    def update_item(self, TableName, Key, UpdateExpression, ExpressionAttributeValues, ExpressionAttributeNames=None, ReturnValues=None, **_):
        assert UpdateExpression == "ADD #n :one" and ExpressionAttributeNames == {"#n": "n"}
        with self._lock:
            self._enter("update_item", TableName)
            item = self.items.setdefault(self._key(Key), dict(Key))
            item["n"] = {"N": str(int(item.get("n", {"N": "0"})["N"]) + int(ExpressionAttributeValues[":one"]["N"]))}
            return {"Attributes": {"n": item["n"]}}

    def query(self, TableName, KeyConditionExpression, ExpressionAttributeValues, ScanIndexForward=True, Limit=None, **_):
        assert KeyConditionExpression == "pk = :pk AND begins_with(sk, :prefix)"
        with self._lock:
            self._enter("query", TableName)
            pk, prefix = ExpressionAttributeValues[":pk"]["S"], ExpressionAttributeValues[":prefix"]["S"]
            rows = sorted(((k, v) for k, v in self.items.items() if k[0] == pk and k[1].startswith(prefix)), reverse=not ScanIndexForward)
        return {"Items": [dict(v) for _, v in rows[: Limit or None]]}
