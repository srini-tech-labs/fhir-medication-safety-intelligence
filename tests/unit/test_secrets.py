"""backend/app/secrets.py: the Lambda-only Secrets Manager bootstrap for OPENAI_API_KEY. No real AWS call --
a fake boto3 Secrets Manager client throughout. Proves: fetches once, populates the SAME env var the SDK
already reads (so redact.py's existing exact-value redaction covers it automatically), is a no-op when the
env var is already set or no secret ARN is configured, and never logs the fetched value.
"""
from __future__ import annotations

import logging

import pytest

from app.secrets import load_secret_env

SECRET_ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:medsafety/openai-abc123"
SECRET_VALUE = "sk-proj-ONLY-IN-SECRETS-MANAGER-NOT-A-REAL-KEY-0000000000"


class FakeSecretsManagerClient:
    def __init__(self, value=SECRET_VALUE):
        self.calls = []
        self._value = value

    def get_secret_value(self, SecretId):
        self.calls.append(SecretId)
        return {"SecretString": self._value, "ARN": SecretId, "Name": "medsafety/openai"}


def patch_boto3(monkeypatch, client):
    import boto3

    monkeypatch.setattr(boto3, "client", lambda service_name: client if service_name == "secretsmanager" else None)


def test_fetches_and_populates_the_target_env_var(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    client = FakeSecretsManagerClient()
    patch_boto3(monkeypatch, client)
    fetched = load_secret_env("OPENAI_API_KEY", SECRET_ARN)
    assert fetched is True
    assert __import__("os").environ["OPENAI_API_KEY"] == SECRET_VALUE
    assert client.calls == [SECRET_ARN]


def test_is_a_noop_when_the_env_var_is_already_set(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "already-set-locally")
    client = FakeSecretsManagerClient()
    patch_boto3(monkeypatch, client)
    fetched = load_secret_env("OPENAI_API_KEY", SECRET_ARN)
    assert fetched is False
    assert client.calls == []  # never even constructs a request -- local/explicit override always wins
    assert __import__("os").environ["OPENAI_API_KEY"] == "already-set-locally"


def test_is_a_noop_when_no_secret_arn_is_configured(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    client = FakeSecretsManagerClient()
    patch_boto3(monkeypatch, client)
    fetched = load_secret_env("OPENAI_API_KEY", None)
    assert fetched is False
    assert client.calls == []


def test_fetches_only_once_per_env_var_env_wins_after_first_fetch(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    client = FakeSecretsManagerClient()
    patch_boto3(monkeypatch, client)
    assert load_secret_env("OPENAI_API_KEY", SECRET_ARN) is True
    assert load_secret_env("OPENAI_API_KEY", SECRET_ARN) is False  # env var now set; second call is a no-op
    assert client.calls == [SECRET_ARN]  # exactly one real fetch


def test_never_logs_the_fetched_secret_value(monkeypatch, caplog):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    patch_boto3(monkeypatch, FakeSecretsManagerClient())
    with caplog.at_level(logging.DEBUG):
        load_secret_env("OPENAI_API_KEY", SECRET_ARN)
    assert SECRET_VALUE not in caplog.text


def test_a_secrets_manager_failure_propagates_for_the_caller_to_handle(monkeypatch):
    """load_secret_env() itself does not swallow errors -- factory.py's caller is responsible for the
    non-blocking degrade-to-mock behavior (tested in test_openai_explanation.py); this proves the failure
    is at least a real, catchable exception, never silently ignored."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    class Denied(Exception):
        pass

    class DeniedClient:
        def get_secret_value(self, SecretId):
            raise Denied("access denied")

    patch_boto3(monkeypatch, DeniedClient())
    with pytest.raises(Denied):
        load_secret_env("OPENAI_API_KEY", SECRET_ARN)
