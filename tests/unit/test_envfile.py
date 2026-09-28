"""The .env / providers.env loader used by the live tooling: allow-listed, non-overriding, and silent about values."""
from __future__ import annotations

from app.envfile import load_databricks_cfg, load_env_file

SECRET = "sk-ant-api03-ENVFILETESTSECRET000000000000"


def test_loads_only_allowlisted_names_and_reports_names_not_values(tmp_path, capsys):
    f = tmp_path / ".env"
    f.write_text(f'# comment\n\nANTHROPIC_API_KEY="{SECRET}"\nexport EXPLANATION_MODEL=claude-sonnet-5\nSOME_OTHER=1\nDATABASE_URL=x\n')
    env: dict[str, str] = {}
    loaded = load_env_file(f, env)
    assert loaded == ["ANTHROPIC_API_KEY", "EXPLANATION_MODEL"]
    assert env == {"ANTHROPIC_API_KEY": SECRET, "EXPLANATION_MODEL": "claude-sonnet-5"}  # quotes/export handled; others ignored
    assert SECRET not in "".join(loaded) and SECRET not in capsys.readouterr().out


def test_existing_environment_wins_and_empty_values_are_ignored(tmp_path):
    f = tmp_path / ".env"
    f.write_text(f"ANTHROPIC_API_KEY={SECRET}\nEXPLANATION_MODEL=\nANTHROPIC_AUTH_TOKEN=\n")
    env = {"ANTHROPIC_API_KEY": "already-exported-in-shell"}
    assert load_env_file(f, env) == []
    assert env == {"ANTHROPIC_API_KEY": "already-exported-in-shell"}


def test_missing_file_is_a_noop(tmp_path):
    env: dict[str, str] = {}
    assert load_env_file(tmp_path / "nope.env", env) == [] and env == {}


def test_the_shipped_template_and_gitignore_are_safe():
    from app.config import REPO_ROOT
    template = (REPO_ROOT / ".env.example").read_text()
    assert "ANTHROPIC_API_KEY=\n" in template and "sk-ant" not in template
    assert ".env" in (REPO_ROOT / ".gitignore").read_text().splitlines()


# ---- multi-provider credentials (.local/providers.env) -------------------------------------------------------
def test_providers_env_loads_openai_gemini_and_databricks_names(tmp_path):
    f = tmp_path / "providers.env"
    f.write_text("OPENAI_API_KEY=sk-test-openai\nGEMINI_API_KEY=fake-gemini\n"
                 "DATABRICKS_HOST=https://x.databricks.com\nDATABRICKS_TOKEN=tok\nDATABRICKS_ENDPOINT=ep\n")
    env: dict[str, str] = {}
    loaded = load_env_file(f, env)
    assert sorted(loaded) == ["DATABRICKS_ENDPOINT", "DATABRICKS_HOST", "DATABRICKS_TOKEN", "GEMINI_API_KEY", "OPENAI_API_KEY"]


def test_the_shipped_local_example_templates_are_safe():
    from app.config import REPO_ROOT
    template = (REPO_ROOT / ".local.example" / "providers.env.example").read_text()
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "DATABRICKS_HOST", "DATABRICKS_TOKEN"):
        assert f"{name}=" in template
    assert "sk-ant" not in template and "sk-proj" not in template and "AIza" not in template
    cfg_template = (REPO_ROOT / ".local.example" / "databricks.cfg.example").read_text()
    assert "[DEFAULT]" in cfg_template and "dapi_your_personal_access_token_here" in cfg_template
    assert ".local/" in (REPO_ROOT / ".gitignore").read_text().splitlines()


# ---- Databricks fallback config file (project-local only, never ~/.databrickscfg) --------------------------------
def test_load_databricks_cfg_fills_host_and_token_from_the_default_section(tmp_path):
    f = tmp_path / "databricks.cfg"
    f.write_text("[DEFAULT]\nhost = https://example.cloud.databricks.com\ntoken = dapi-fake-token\n")
    env: dict[str, str] = {}
    assert load_databricks_cfg(f, env) == ["DATABRICKS_HOST", "DATABRICKS_TOKEN"]
    assert env == {"DATABRICKS_HOST": "https://example.cloud.databricks.com", "DATABRICKS_TOKEN": "dapi-fake-token"}


def test_load_databricks_cfg_does_not_override_existing_env(tmp_path):
    f = tmp_path / "databricks.cfg"
    f.write_text("[DEFAULT]\nhost = https://example.cloud.databricks.com\ntoken = dapi-fake-token\n")
    env = {"DATABRICKS_HOST": "already-set"}
    loaded = load_databricks_cfg(f, env)
    assert loaded == ["DATABRICKS_TOKEN"] and env["DATABRICKS_HOST"] == "already-set"


def test_load_databricks_cfg_missing_file_is_a_noop(tmp_path):
    env: dict[str, str] = {}
    assert load_databricks_cfg(tmp_path / "nope.cfg", env) == [] and env == {}
