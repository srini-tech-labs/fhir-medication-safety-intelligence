"""Regression proof that secret-bearing paths (.local/, .env*, .aws-local/, Databricks config) can never enter
git-tracked files. Complements test_lambda_package.py (Lambda zip) and test_redaction.py (logs)."""
from __future__ import annotations

import subprocess

import pytest

from app.config import REPO_ROOT

SECRET_PATTERNS = (".local/", ".env", ".aws-local/", ".env.aws")
SECRET_PROBE_PATHS = (".local/providers.env", ".local/databricks.cfg", ".env", ".env.aws")


def run_git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=True).stdout


def test_gitignore_lists_every_secret_pattern():
    gitignore = (REPO_ROOT / ".gitignore").read_text("utf-8")
    lines = {line.strip() for line in gitignore.splitlines()}
    for pattern in SECRET_PATTERNS:
        assert pattern in lines, f"{pattern!r} is not in .gitignore"


def test_no_tracked_file_is_under_a_secret_path():
    tracked = run_git("ls-files").splitlines()
    for path in tracked:
        assert not path.startswith(".local/"), f"{path} is tracked but under .local/"
        assert path not in (".env", ".env.aws"), f"{path} is tracked but is a secrets file"
        assert not path.startswith(".aws-local/"), f"{path} is tracked but under .aws-local/"
    # the committed, secret-free example templates ARE expected to be tracked (sanity check the assertions above
    # are not accidentally vacuous by requiring the sibling directory to exist and be tracked)
    assert any(p.startswith(".local.example/") for p in tracked)


@pytest.mark.parametrize("path", SECRET_PROBE_PATHS)
def test_git_check_ignore_matches_every_secret_path_even_if_it_does_not_exist_on_disk(path):
    proc = subprocess.run(["git", "check-ignore", "-q", path], cwd=REPO_ROOT)
    assert proc.returncode == 0, f"git does not ignore {path!r}"
