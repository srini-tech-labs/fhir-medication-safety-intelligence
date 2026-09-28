"""Minimal, allow-listed .env / providers.env loader for the opt-in live tooling (never used by the API server).

Values are never printed or returned; only the *names* that were set are reported. Variables already present in
the environment win, so a shell ``export`` or ``EXPLANATION_MODEL=... make ...`` always overrides the file --
process environment is always an optional override, the file is the primary, repeatable source.
"""
from __future__ import annotations

import configparser
import os
from pathlib import Path
from typing import MutableMapping

ALLOWED = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "EXPLANATION_MODEL",
          "OPENAI_API_KEY", "GEMINI_API_KEY", "DATABRICKS_HOST", "DATABRICKS_TOKEN", "DATABRICKS_ENDPOINT")


def load_env_file(path: Path, environ: MutableMapping[str, str] | None = None,
                  allowed: tuple[str, ...] = ALLOWED) -> list[str]:
    """Load KEY=VALUE lines for allow-listed names that are not already set. Returns the names that were set."""
    environ = os.environ if environ is None else environ
    if not path.is_file():
        return []
    loaded: list[str] = []
    for raw in path.read_text("utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        name, _, value = line.partition("=")
        name, value = name.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if name in allowed and value and not environ.get(name):
            environ[name] = value
            loaded.append(name)
    return loaded


def load_databricks_cfg(path: Path, environ: MutableMapping[str, str] | None = None) -> list[str]:
    """Fallback for Databricks credentials: a project-local ``[DEFAULT]`` host/token file (same shape as the
    real ``~/.databrickscfg``, but never that file itself -- it is not read by any code in this repo). Only
    consulted by callers when DATABRICKS_HOST/DATABRICKS_TOKEN are not already set (env or providers.env)."""
    environ = os.environ if environ is None else environ
    if not path.is_file():
        return []
    parser = configparser.ConfigParser()
    parser.read(path, encoding="utf-8")
    if "DEFAULT" not in parser or not (parser.defaults()):
        return []
    loaded: list[str] = []
    mapping = {"host": "DATABRICKS_HOST", "token": "DATABRICKS_TOKEN"}
    for key, name in mapping.items():
        value = parser["DEFAULT"].get(key, "").strip()
        if value and not environ.get(name):
            environ[name] = value
            loaded.append(name)
    return loaded
