"""AWS Secrets Manager bootstrap for credentials that don't come through the AWS credential chain (e.g. a
third-party API key like OpenAI's). Used only by the deployed Lambda; local dev and the eval harness never
configure a ``*_SECRET_ARN`` and so never touch this module at all -- they set the credential env var
directly (shell export or ``.local/providers.env``).

The fetched value is written into the SAME environment variable the provider's SDK already reads
(``os.environ[env_var]``), not kept in a separate cache -- so the existing ``redact.py`` exact-value
redaction (``_CREDENTIAL_ENV``) covers it automatically with no separate redaction path to keep in sync,
and every provider's ``_get_client()`` (which already reads ``os.getenv(env_var)``) needs no change at all.
Fetched at most once per process (cached via the env var itself surviving for the life of the Lambda
execution environment, including warm invocations) and never logged -- this module never calls
``log.info``/``print`` with the secret value, and ``boto3``'s own request logging is already pinned to
WARNING by ``harden_sdk_logging()`` (``botocore`` is in ``_SDK_LOGGERS``).
"""
from __future__ import annotations

import os


def load_secret_env(env_var: str, secret_arn: str | None) -> bool:
    """If ``env_var`` isn't already set and ``secret_arn`` names a Secrets Manager secret, fetch its
    ``SecretString`` and populate ``env_var`` with it. A no-op (returns ``False``) if ``env_var`` is already
    set (local dev / an explicit override always wins) or ``secret_arn`` is falsy. Returns ``True`` only
    when a fetch actually happened. Never raises for a missing/inaccessible secret in a way that leaks
    detail -- the caller (``factory.create_explanation_service``) already falls back to the deterministic
    mock on any exception from provider construction, and that fallback path is redacted the same as any
    other provider-construction failure.
    """
    if os.getenv(env_var) or not secret_arn:
        return False
    import boto3  # already a core dependency (DynamoDB app state, HealthLake SigV4); no new package needed

    client = boto3.client("secretsmanager")
    value = client.get_secret_value(SecretId=secret_arn)["SecretString"]
    os.environ[env_var] = value
    return True
