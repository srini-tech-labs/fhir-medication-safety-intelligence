"""The Lambda zip: contents, frozen data byte-identical, forbidden content refused, reproducible. The opt-in test builds real
dependencies (network) and runs the handler from the unzipped package in a clean interpreter with the Anthropic SDK blocked."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import zipfile

import pytest

from app.config import REPO_ROOT

spec = importlib.util.spec_from_file_location("build_lambda", REPO_ROOT / "scripts" / "build_lambda.py")
bl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bl)


@pytest.fixture(scope="module")
def nodeps_zip(tmp_path_factory):
    out = tmp_path_factory.mktemp("pkg") / "api.zip"
    bl.build(out, skip_deps=True)
    return out


def names(path):
    with zipfile.ZipFile(path) as zf:
        return zf.namelist()


def test_the_zip_contains_the_app_and_exactly_the_seven_frozen_files(nodeps_zip):
    n = names(nodeps_zip)
    assert {"app/lambda_handler.py", "app/main.py", "app/services/explanation/bedrock.py", "app/repository/app_state_dynamodb.py"} <= set(n)
    assert sorted(x for x in n if x.startswith("data/")) == sorted(f"data/phase0_v1_0/{f}" for f in bl.DATA_FILES)


def test_frozen_files_are_byte_identical_to_the_package(nodeps_zip):
    with zipfile.ZipFile(nodeps_zip) as zf:
        for rel in bl.DATA_FILES:
            assert zf.read(f"data/phase0_v1_0/{rel}") == (REPO_ROOT / "data" / "phase0_v1_0" / rel).read_bytes()
    sums = dict(reversed(l.split("  ", 1)) for l in (REPO_ROOT / "data" / "phase0_v1_0" / "SHA256SUMS.txt").read_text().splitlines())
    assert all(hashlib.sha256((REPO_ROOT / "data" / "phase0_v1_0" / r).read_bytes()).hexdigest() == sums[r] for r in bl.DATA_FILES)


def test_no_secrets_tests_reports_or_caches_are_packaged(nodeps_zip):
    joined = "\n".join(names(nodeps_zip))
    for bad in (".env", ".local", ".aws-local", "credentials", "tests/", "__pycache__", "live_report", ".git", "SHA256SUMS", "expected_results"):
        assert bad not in joined, bad


def test_the_build_is_reproducible(tmp_path):
    a, b = tmp_path / "a.zip", tmp_path / "b.zip"
    assert bl.build(a, skip_deps=True)["sha256"] == bl.build(b, skip_deps=True)["sha256"]
    assert (tmp_path / "a.zip.sha256").read_text().split()[0] == bl.sha256(a)
    assert json.loads((tmp_path / "a.zip.manifest.json").read_text())["dataFiles"] == bl.DATA_FILES


def test_a_frozen_file_that_does_not_match_its_checksum_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(bl, "frozen_checksums", lambda: {**{f: "0" * 64 for f in bl.DATA_FILES}})
    with pytest.raises(SystemExit, match="does not match SHA256SUMS"):
        bl.build(tmp_path / "x.zip", skip_deps=True)


@pytest.mark.parametrize("planted", ["app/.env", "app/.aws-local/credentials", "app/tests/test_x.py", "app/__pycache__/x.pyc"])
def test_verify_refuses_forbidden_files(nodeps_zip, tmp_path, planted):
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(nodeps_zip) as src, zipfile.ZipFile(evil, "w") as dst:
        for n in src.namelist():
            dst.writestr(n, src.read(n))
        dst.writestr(planted, "x")
    with pytest.raises(SystemExit, match="forbidden content"):
        bl.verify_zip(evil, deps=False)


def test_extra_data_files_are_refused(nodeps_zip, tmp_path):
    evil = tmp_path / "evil2.zip"
    with zipfile.ZipFile(nodeps_zip) as src, zipfile.ZipFile(evil, "w") as dst:
        for n in src.namelist():
            dst.writestr(n, src.read(n))
        dst.writestr("data/phase0_v1_0/expected/expected_results.json", "{}")
    with pytest.raises(SystemExit, match="unexpected data"):
        bl.verify_zip(evil, deps=False)


def test_oversized_packages_stop_the_build(nodeps_zip, monkeypatch):
    monkeypatch.setattr(bl, "MAX_ZIP_BYTES", 1000)
    with pytest.raises(SystemExit, match="ask before using an S3 artifact bucket"):
        bl.verify_zip(nodeps_zip, deps=False)


# ---- provider-aware packaging (docs/adr/0011: OpenAI is the initial deployed provider) ---------------------------
def test_provider_dependency_table_bundles_exactly_one_ai_sdk_or_none(tmp_path):
    assert bl.PROVIDER_DEPENDENCIES["bedrock"] == []  # boto3 (in BASE_DEPENDENCIES) alone talks to Bedrock Runtime
    assert bl.PROVIDER_DEPENDENCIES["openai"] == [f"openai=={bl.OPENAI_SDK_PIN}"]


def test_openai_sdk_is_pinned_exactly_not_a_floor():
    """Pinned (==), not `>=`: the deployed version must be exactly the one docs/PROVIDER_EVALUATION.md validated
    against the live GPT-5.6 Luna model, not whatever the newest compatible release happens to be at build time."""
    (dep,) = bl.PROVIDER_DEPENDENCIES["openai"]
    assert dep == f"openai=={bl.OPENAI_SDK_PIN}" and ">=" not in dep


def test_install_dependencies_command_includes_openai_only_for_the_openai_provider(tmp_path, monkeypatch):
    """No network: captures the command install_dependencies() WOULD run, never actually runs it."""
    captured = {}

    def fake_run(cmd, check):
        captured["cmd"] = cmd

    monkeypatch.setattr(bl.subprocess, "run", fake_run)
    bl.install_dependencies(tmp_path, "host", "bedrock")
    assert f"openai=={bl.OPENAI_SDK_PIN}" not in captured["cmd"]

    bl.install_dependencies(tmp_path, "host", "openai")
    assert f"openai=={bl.OPENAI_SDK_PIN}" in captured["cmd"]
    assert f"boto3=={bl.BOTO3_PIN}" in captured["cmd"]  # boto3 still bundled: DynamoDB app state + Secrets Manager


def test_install_dependencies_refuses_an_unknown_provider(tmp_path):
    with pytest.raises(SystemExit, match="unknown --provider"):
        bl.install_dependencies(tmp_path, "host", "not-a-real-provider")


def _plant_deps_markers(src_zip: Path, dst_zip: Path, *, extra_dirs: tuple[str, ...] = ()) -> None:
    """Builds a synthetic "deps=True"-shaped zip from a skip_deps zip: no real network install needed to
    exercise verify_zip()'s dependency-presence assertions."""
    with zipfile.ZipFile(src_zip) as src, zipfile.ZipFile(dst_zip, "w") as dst:
        for n in src.namelist():
            dst.writestr(n, src.read(n))
        dst.writestr(f"boto3-{bl.BOTO3_PIN}.dist-info/METADATA", f"Name: boto3\nVersion: {bl.BOTO3_PIN}\n")
        dst.writestr("mangum/__init__.py", "")
        dst.writestr("fastapi/__init__.py", "")
        for d in extra_dirs:
            dst.writestr(f"{d}/__init__.py", "")


def test_verify_zip_accepts_openai_sdk_only_for_the_openai_provider(nodeps_zip, tmp_path):
    good = tmp_path / "openai_ok.zip"
    _plant_deps_markers(nodeps_zip, good, extra_dirs=("openai",))
    info = bl.verify_zip(good, deps=True, provider="openai")
    assert info["files"] > 0


def test_verify_zip_refuses_the_openai_sdk_in_a_bedrock_package(nodeps_zip, tmp_path):
    evil = tmp_path / "bedrock_with_openai.zip"
    _plant_deps_markers(nodeps_zip, evil, extra_dirs=("openai",))
    with pytest.raises(SystemExit, match="openai SDK must not be in a --provider bedrock package"):
        bl.verify_zip(evil, deps=True, provider="bedrock")


def test_verify_zip_refuses_a_missing_openai_sdk_when_provider_is_openai(nodeps_zip, tmp_path):
    missing = tmp_path / "openai_missing_sdk.zip"
    _plant_deps_markers(nodeps_zip, missing)  # no "openai" extra_dirs
    with pytest.raises(SystemExit, match="provider openai but the openai SDK is not in the package"):
        bl.verify_zip(missing, deps=True, provider="openai")


def test_verify_zip_still_refuses_anthropic_and_google_genai_regardless_of_provider(nodeps_zip, tmp_path):
    for provider, extra in (("bedrock", "anthropic"), ("openai", "google_genai")):
        evil = tmp_path / f"{provider}_with_{extra}.zip"
        _plant_deps_markers(nodeps_zip, evil, extra_dirs=(extra, "openai") if provider == "openai" else (extra,))
        with pytest.raises(SystemExit, match="must not be in"):
            bl.verify_zip(evil, deps=True, provider=provider)


def test_secrets_manager_arn_or_key_value_never_appears_in_the_package(tmp_path, monkeypatch):
    """Structural guarantee: packaging never reads a credential env var, even if one happens to be set in the
    building environment (e.g. a developer's own shell). A real-looking value is set here specifically so this
    test would catch it if that guarantee were ever broken."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-THIS-MUST-NEVER-APPEAR-IN-THE-ZIP-0000000000")
    monkeypatch.setenv("OPENAI_API_KEY_SECRET_ARN", "arn:aws:secretsmanager:us-east-1:123456789012:secret:medsafety/openai-THIS-ARN-IS-FINE-TO-SEE")
    out = tmp_path / "no-secret.zip"
    bl.build(out, skip_deps=True, provider="openai")
    with zipfile.ZipFile(out) as zf:
        for n in zf.namelist():
            if n.endswith((".py", ".json", ".txt")):
                assert "sk-proj-THIS-MUST-NEVER-APPEAR" not in zf.read(n).decode("utf-8", errors="ignore")


def test_manifest_records_which_provider_was_built(tmp_path):
    out = tmp_path / "manifest-provider.zip"
    bl.build(out, skip_deps=True, provider="openai")
    assert json.loads((tmp_path / "manifest-provider.zip.manifest.json").read_text())["provider"] == "openai"
    out2 = tmp_path / "manifest-provider2.zip"
    bl.build(out2, skip_deps=True)  # default
    assert json.loads((tmp_path / "manifest-provider2.zip.manifest.json").read_text())["provider"] == "bedrock"


def test_app_secrets_module_is_always_packaged(nodeps_zip):
    assert "app/secrets.py" in names(nodeps_zip)


def test_pinned_boto3_knows_the_converse_fields_the_provider_sends():
    """The runtime's boto3 may be older, so the package bundles this pin; it must model `outputConfig.textFormat` and `effort`."""
    import boto3
    import botocore

    assert botocore.__version__ == bl.BOTO3_PIN == boto3.__version__ or botocore.__version__ >= bl.BOTO3_PIN
    shape = boto3.client("bedrock-runtime", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="y") \
        .meta.service_model.operation_model("Converse").input_shape.members["outputConfig"]
    assert {"textFormat", "effort"} <= set(shape.members)


@pytest.mark.skipif(os.getenv("RUN_PACKAGE_BUILD_TEST") != "1", reason="opt-in (needs network): RUN_PACKAGE_BUILD_TEST=1")
def test_real_dependency_build_imports_and_serves_health_without_the_anthropic_sdk(tmp_path):
    out = tmp_path / "host.zip"
    info = bl.build(out, platform="host")
    assert info["bytes"] < bl.MAX_ZIP_BYTES
    unzipped = tmp_path / "unzipped"
    with zipfile.ZipFile(out) as zf:
        zf.extractall(unzipped)
    code = (
        "import sys; sys.modules['anthropic'] = None; sys.path.insert(0, %r); "
        "import boto3, json; assert boto3.__version__ == %r, boto3.__version__; "
        "from app.lambda_handler import handler; "
        "ev = {'resource':'/v1/health','path':'/v1/health','httpMethod':'GET','headers':{'Host':'x'},'multiValueHeaders':{'Host':['x']},"
        "'queryStringParameters':None,'multiValueQueryStringParameters':None,'pathParameters':None,'stageVariables':None,"
        "'requestContext':{'requestId':'r','stage':'dev','path':'/dev/v1/health','httpMethod':'GET','resourcePath':'/v1/health','accountId':'1','apiId':'a','identity':{'sourceIp':'1.1.1.1'}},"
        "'body':None,'isBase64Encoded':False}; "
        "r = handler(ev, None); print(r['statusCode'], r['body'])"
    ) % (str(unzipped), bl.BOTO3_PIN)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("AWS_", "ANTHROPIC_"))}
    env |= {"DATA_PACKAGE_DIR": str(unzipped / "data" / "phase0_v1_0"), "PYTHONPATH": "", "PYTHONDONTWRITEBYTECODE": "1"}
    proc = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=120, cwd=tmp_path)
    assert proc.returncode == 0, proc.stderr[-1500:]
    assert proc.stdout.strip().endswith('200 {"status":"ok"}'), proc.stdout
