#!/usr/bin/env python
"""Build the Lambda deployment zip for the API (`app.lambda_handler.handler`).

    backend/.venv/bin/python scripts/build_lambda.py --out build/medsafety-api.zip                       # bedrock (default), arm64
    backend/.venv/bin/python scripts/build_lambda.py --out build/medsafety-api.zip --provider openai      # openai, arm64 -- see docs/adr/0011
    backend/.venv/bin/python scripts/build_lambda.py --out /tmp/x.zip --platform host                    # host wheels (local import smoke test)
    backend/.venv/bin/python scripts/build_lambda.py --out /tmp/x.zip --skip-deps                        # app + data only (unit tests)

What goes in (and only this):
  * `app/`                      the FastAPI application (no __pycache__) -- ALL provider modules ship regardless of
                                `--provider`; only `EXPLANATION_PROVIDER` (a Lambda env var) picks which runs.
  * `data/phase0_v1_0/...`      the 7 frozen files the runtime reads (4 rule files, 2 terminology files, the system prompt),
                                copied byte-for-byte and checked against SHA256SUMS.txt
  * dependencies                fastapi, pydantic, mangum, httpx and a PINNED boto3/botocore always (DynamoDB app state,
                                HealthLake SigV4, and -- for `--provider openai` -- fetching OPENAI_API_KEY from Secrets
                                Manager, see app/secrets.py); `--provider openai` additionally bundles `openai` (the
                                deployed provider's SDK). `--provider bedrock` (default) bundles neither `anthropic` nor
                                `openai`/`google-genai` -- the Bedrock path uses boto3 only, and exactly one evaluation-
                                candidate SDK is ever bundled, matching whichever provider is actually being deployed.
Never included: tests, `.env*`, `.local`, `.aws-local`, credentials, reports, the rest of the data package. No secret
VALUE is ever in this script's inputs or outputs -- only `OPENAI_API_KEY_SECRET_ARN` (an ARN, not a secret) is a Lambda
env var; the key itself is fetched from Secrets Manager at runtime (app/secrets.py), never baked into the zip.
The zip is reproducible (sorted entries, fixed timestamps) and is accompanied by `<zip>.sha256` and `<zip>.manifest.json`.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PACKAGE = REPO / "data" / "phase0_v1_0"
DATA_FILES = [
    "rules/data_gap_rules.json", "rules/drug_drug_rules.json", "rules/drug_lab_rules.json", "rules/rule_evidence.json",
    "terminology/loinc_mapping.json", "terminology/rxnorm_mapping.json", "prompts/ai_explanation_system.txt",
]
BOTO3_PIN = "1.43.98"  # knows Converse outputConfig.textFormat / effort (asserted by tests/unit/test_lambda_package.py)
# The exact `openai` SDK version installed in backend/.venv when the GPT-5.6 Luna live evaluation
# (docs/PROVIDER_EVALUATION.md: 5/5 schema success, 5/5 guard acceptance, live_report_openai.json) passed.
# Pinned, not a floor (`>=`), because that evaluation is what validated this SDK's request/response shape
# against the deployed model; bumping this pin means re-running that evaluation, not just a routine upgrade.
OPENAI_SDK_PIN = "3.18.0"
BASE_DEPENDENCIES = ["fastapi>=0.115", "pydantic>=2.7", "mangum>=0.17", "httpx>=0.27", f"boto3=={BOTO3_PIN}", f"botocore=={BOTO3_PIN}"]
PROVIDERS = ("bedrock", "openai")
# Which evaluation-candidate SDK (if any) `--provider` bundles. `bedrock` (default) needs none: boto3 alone talks to
# Bedrock Runtime. Never both -- exactly one AI SDK, matching the one provider actually selected for that build.
PROVIDER_DEPENDENCIES = {"bedrock": [], "openai": [f"openai=={OPENAI_SDK_PIN}"]}
FORBIDDEN_PARTS = ("tests/", ".env", ".local", ".aws-local", "credentials", "__pycache__", ".pytest_cache", "live_report", "SHA256SUMS", ".git")
MAX_ZIP_BYTES = 50 * 1024 * 1024  # Lambda direct-upload limit; above this I stop and ask before adding an S3 artifact bucket
ZIP_TIME = (2026, 1, 1, 0, 0, 0)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def frozen_checksums() -> dict[str, str]:
    out = {}
    for line in (PACKAGE / "SHA256SUMS.txt").read_text("utf-8").splitlines():
        digest, _, name = line.partition("  ")
        out[name.strip()] = digest
    return out


def copy_data(stage: Path) -> None:
    sums = frozen_checksums()
    for rel in DATA_FILES:
        src = PACKAGE / rel
        if sha256(src) != sums[rel]:
            raise SystemExit(f"STOP: frozen file {rel} does not match SHA256SUMS.txt; refusing to package it")
        dst = stage / "data" / "phase0_v1_0" / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        if sha256(dst) != sums[rel]:
            raise SystemExit(f"STOP: copy of {rel} differs from the frozen file")


def install_dependencies(stage: Path, platform: str, provider: str) -> None:
    if provider not in PROVIDERS:
        raise SystemExit(f"unknown --provider {provider!r} (valid: {', '.join(PROVIDERS)})")
    cmd = ["uv", "pip", "install", "--target", str(stage), "--python-version", "3.12", "--quiet"]
    if platform == "arm64":
        cmd += ["--python-platform", "aarch64-manylinux2014"]
    elif platform != "host":
        raise SystemExit(f"unknown platform {platform!r}")
    else:
        cmd += ["--python", sys.executable]
    subprocess.run([*cmd, *BASE_DEPENDENCIES, *PROVIDER_DEPENDENCIES[provider]], check=True)


def prune(stage: Path) -> None:
    for pattern in ("**/__pycache__", "**/*.pyc", "**/tests", "**/.pytest_cache"):
        for path in sorted(stage.glob(pattern), reverse=True):
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            elif path.exists():
                path.unlink()
    for name in ("bin",):  # console scripts from dependencies are useless in Lambda
        shutil.rmtree(stage / name, ignore_errors=True)


def write_zip(stage: Path, out: Path) -> list[str]:
    out.parent.mkdir(parents=True, exist_ok=True)
    names = sorted(str(p.relative_to(stage)) for p in stage.rglob("*") if p.is_file())
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for name in names:
            info = zipfile.ZipInfo(name, ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, (stage / name).read_bytes())
    return names


def verify_zip(zip_path: Path, *, deps: bool, provider: str = "bedrock") -> dict:
    """Assertions on the finished artifact. Raises SystemExit with the first violation."""
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        own = [n for n in names if n.startswith(("app/", "data/")) or "/" not in n]  # what WE ship (dependencies legitimately contain e.g. botocore/credentials.py)
        for bad in FORBIDDEN_PARTS:
            hit = [n for n in own if bad in n]
            if hit:
                raise SystemExit(f"STOP: forbidden content in package ({bad}): {hit[:3]}")
        # No secret VALUE is ever an input to this script (packaging never reads a credential env var or
        # .local/providers.env -- confirmed by test_secrets_manager_arn_or_key_value_never_appears_in_the_package,
        # which builds with a real-looking OPENAI_API_KEY set in the environment and asserts it doesn't leak
        # in). A content scan here would be a red herring: backend/app/redact.py itself legitimately spells
        # out key-shaped substrings (e.g. "sk-ant-", "dapi") as regex source, so pattern-matching file
        # content can't distinguish "an embedded secret" from "the code that detects secret shapes" -- the
        # guarantee that matters is structural (nothing here ever touches a real value), not textual.
        sums = frozen_checksums()
        for rel in DATA_FILES:
            body = zf.read(f"data/phase0_v1_0/{rel}")
            if hashlib.sha256(body).hexdigest() != sums[rel]:
                raise SystemExit(f"STOP: {rel} in the zip differs from the frozen file")
        extra_data = [n for n in names if n.startswith("data/") and n[len("data/phase0_v1_0/"):] not in DATA_FILES]
        if extra_data:
            raise SystemExit(f"STOP: unexpected data files in the package: {extra_data[:3]}")
        for required in ("app/lambda_handler.py", "app/main.py", "app/repository/healthlake.py",
                         "app/services/explanation/bedrock.py", "app/services/explanation/openai.py", "app/secrets.py"):
            if required not in names:
                raise SystemExit(f"STOP: {required} missing from the package")
        info = {"files": len(names), "bytes": zip_path.stat().st_size}
        if deps:
            if any(n.startswith("anthropic/") for n in names):
                raise SystemExit("STOP: the Anthropic SDK must not be in the package")
            if any(n.startswith(("google_genai/", "google/genai/")) for n in names):
                raise SystemExit("STOP: the evaluation-only google-genai SDK must not be in the package")
            has_openai_sdk = any(n.startswith("openai/") for n in names)
            if provider == "openai" and not has_openai_sdk:
                raise SystemExit("STOP: --provider openai but the openai SDK is not in the package")
            if provider != "openai" and has_openai_sdk:
                raise SystemExit(f"STOP: the openai SDK must not be in a --provider {provider} package")
            meta = [n for n in names if n.startswith("boto3-") and n.endswith("/METADATA")]
            if not meta or f"Version: {BOTO3_PIN}" not in zf.read(meta[0]).decode():
                raise SystemExit(f"STOP: boto3 {BOTO3_PIN} is not what got bundled")
            if "mangum/__init__.py" not in names or "fastapi/__init__.py" not in names:
                raise SystemExit("STOP: mangum/fastapi missing from the package")
    if info["bytes"] > MAX_ZIP_BYTES:
        raise SystemExit(f"STOP: zip is {info['bytes']:,} bytes (> {MAX_ZIP_BYTES:,}); ask before using an S3 artifact bucket")
    return info


def build(out: Path, *, platform: str = "arm64", skip_deps: bool = False, provider: str = "bedrock") -> dict:
    if provider not in PROVIDERS:
        raise SystemExit(f"unknown --provider {provider!r} (valid: {', '.join(PROVIDERS)})")
    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp) / "stage"
        stage.mkdir()
        if not skip_deps:
            install_dependencies(stage, platform, provider)
        shutil.copytree(REPO / "backend" / "app", stage / "app", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        copy_data(stage)
        prune(stage)
        write_zip(stage, out)
    info = verify_zip(out, deps=not skip_deps, provider=provider)
    digest = sha256(out)
    Path(f"{out}.sha256").write_text(f"{digest}  {out.name}\n")
    Path(f"{out}.manifest.json").write_text(json.dumps({"sha256": digest, **info, "platform": "none" if skip_deps else platform,
                                                         "provider": provider, "boto3": None if skip_deps else BOTO3_PIN,
                                                         "dataFiles": DATA_FILES}, indent=2))
    return {"sha256": digest, **info}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--platform", choices=["arm64", "host"], default="arm64")
    ap.add_argument("--skip-deps", action="store_true")
    ap.add_argument("--provider", choices=list(PROVIDERS), default="bedrock",
                    help="which AI-explanation SDK to bundle (default: bedrock, i.e. none -- boto3 only)")
    args = ap.parse_args()
    info = build(args.out, platform=args.platform, skip_deps=args.skip_deps, provider=args.provider)
    print(f"built {args.out}  {info['bytes']:,} bytes  {info['files']} files  sha256={info['sha256']}  provider={args.provider}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
