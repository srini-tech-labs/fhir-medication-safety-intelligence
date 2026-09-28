"""Static checks of the rendered IAM/KMS/S3 policies against the approved Phase 3 plan (no AWS access)."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

from app.config import REPO_ROOT

HL = REPO_ROOT / "infrastructure" / "aws" / "healthlake"
sys.path.insert(0, str(HL))
from render_policy import render  # noqa: E402

ACCT, DS = "123456789012", "0123456789abcdef0123456789abcdef"
KEY = f"arn:aws:kms:us-east-1:{ACCT}:key/11111111-2222-3333-4444-555555555555"
ENV = {"ACCT": ACCT, "DS": DS, "KEY_ARN": KEY, "IMPORT_BUCKET": f"medsafety-hl-import-{ACCT}-us-east-1",
       "RESULTS_BUCKET": f"medsafety-hl-results-{ACCT}-us-east-1", "BUCKET": "b",
       "PRINCIPAL_ARN": f"arn:aws:iam::{ACCT}:role/aws-reserved/sso.amazonaws.com/AWSReservedSSO_Admin_abc"}
DS_ARN = f"arn:aws:healthlake:us-east-1:{ACCT}:datastore/fhir/{DS}"
APP_ACTIONS = {"healthlake:ReadResource", "healthlake:SearchWithGet", "healthlake:SearchWithPost", "healthlake:GetCapabilities",
               "healthlake:CreateResource", "healthlake:UpdateResource"}
VERIFIER_ONLY = {"healthlake:GetHistoryByResourceId", "healthlake:VersionReadResource", "healthlake:ProcessBundle"}


def load(name: str) -> dict:
    return json.loads(render((HL / "policies" / name).read_text("utf-8"), ENV))


def statements(doc: dict) -> list[dict]:
    return doc["Statement"]


def actions(doc: dict) -> set[str]:
    return {a for s in statements(doc) for a in ([s["Action"]] if isinstance(s["Action"], str) else s["Action"])}


def as_list(x):
    return [x] if isinstance(x, str) else x


TEMPLATES = sorted(p.name for p in (HL / "policies").glob("*.tpl"))


@pytest.mark.parametrize("name", TEMPLATES)
def test_every_template_renders_to_valid_json_with_no_leftover_placeholders(name):
    text = render((HL / "policies" / name).read_text("utf-8"), ENV)
    assert "${" not in text and json.loads(text)


def test_render_fails_loudly_on_a_missing_variable():
    with pytest.raises(KeyError):
        render((HL / "policies" / "import-trust.json.tpl").read_text("utf-8"), {"ACCT": ACCT})


def test_datastore_arns_use_the_documented_fhir_form_everywhere():
    """User correction 1 and 3: `datastore/fhir/<id>` in data-plane resources and the trust condition, never `datastore/<id>`."""
    for name in ("app-access.json.tpl", "verifier-access.json.tpl", "import-trust.json.tpl"):
        text = json.dumps(load(name))
        arns = re.findall(r"arn:aws:healthlake:[^\"]+", text)
        assert arns, name
        for arn in arns:
            assert arn == DS_ARN or arn == f"{DS_ARN}/import-job/*", (name, arn)
        assert f"datastore/{DS}" not in text.replace(f"datastore/fhir/{DS}", "")


def test_import_trust_is_a_single_source_arn_arn_equals_condition():
    (stmt,) = statements(load("import-trust.json.tpl"))
    assert stmt["Principal"] == {"Service": "healthlake.amazonaws.com"} and stmt["Action"] == "sts:AssumeRole"
    assert stmt["Condition"] == {"StringEquals": {"aws:SourceAccount": ACCT}, "ArnEquals": {"aws:SourceArn": DS_ARN}}
    assert json.dumps(stmt).count("aws:SourceArn") == 1  # no second ARN variant


def test_app_role_has_exactly_the_runtime_actions_and_none_of_the_verifier_ones():
    a = actions(load("app-access.json.tpl"))
    hl = {x for x in a if x.startswith("healthlake:")}
    assert hl == APP_ACTIONS | {"healthlake:DescribeFHIRDatastore"}
    assert not hl & VERIFIER_ONLY and "healthlake:DeleteResource" not in hl


def test_verifier_role_is_app_plus_history_vread_bundles_delete_and_import_job_reads():
    app, ver = actions(load("app-access.json.tpl")), actions(load("verifier-access.json.tpl"))
    assert app <= ver
    assert ver - app == VERIFIER_ONLY | {"healthlake:DeleteResource", "healthlake:DescribeFHIRImportJob",
                                         "healthlake:ListFHIRImportJobs", "s3:GetObject", "s3:ListBucket"}
    assert "DecryptImportResults" in {s["Sid"] for s in statements(load("verifier-access.json.tpl"))}


@pytest.mark.parametrize("name", ["app-access.json.tpl", "verifier-access.json.tpl", "import-access.json.tpl"])
def test_role_policies_have_no_wildcard_actions_or_resources(name):
    for s in statements(load(name)):
        assert s["Effect"] == "Allow"
        assert not any(a == "*" or a.endswith(":*") for a in as_list(s["Action"])), s["Sid"]
        assert "*" not in as_list(s["Resource"]), s["Sid"]  # trailing prefixes like .../phase0-v1/* are fine


def test_app_role_kms_use_is_limited_to_the_key_via_healthlake():
    (kms,) = [s for s in statements(load("app-access.json.tpl")) if s["Sid"] == "CmkViaHealthLake"]
    assert kms["Resource"] == KEY and kms["Condition"] == {"StringEquals": {"kms:ViaService": "healthlake.us-east-1.amazonaws.com"}}


def test_import_role_reads_only_the_input_prefix_and_writes_only_the_results_prefix():
    st = {s["Sid"]: s for s in statements(load("import-access.json.tpl"))}
    assert st["ReadInput"]["Action"] == "s3:GetObject" and st["ReadInput"]["Resource"].endswith("/phase0-v1/*")
    assert st["WriteResults"]["Action"] == "s3:PutObject" and st["WriteResults"]["Resource"].startswith("arn:aws:s3:::medsafety-hl-results-")
    assert not any("Delete" in a for s in st.values() for a in as_list(s["Action"]))


def test_principal_trust_names_one_iam_principal_never_root_or_a_session():
    (stmt,) = statements(load("principal-trust.json.tpl"))
    arn = stmt["Principal"]["AWS"]
    assert arn == ENV["PRINCIPAL_ARN"] and not arn.endswith(":root") and ":assumed-role/" not in arn and stmt["Action"] == "sts:AssumeRole"


def test_deployer_policy_is_scoped_by_name_and_tag_not_admin():
    doc = load("deployer-policy.json.tpl")
    star = {s["Sid"] for s in statements(doc) if "*" in as_list(s["Resource"])}
    assert star == {"Identity", "KmsCreateTagged", "KmsListAliases", "RamReadForHealthLakeCreate", "HealthLakeCreateTagged", "HealthLakeList", "CloudTrailReadOnly"}
    for s in statements(doc):
        assert not any(a in ("*", "iam:*", "s3:*", "kms:*", "healthlake:*") for a in as_list(s["Action"])), s["Sid"]
    forbidden = {"iam:AttachRolePolicy", "iam:CreateUser", "iam:CreateAccessKey", "iam:PutUserPolicy", "s3:PutBucketAcl",
                 "healthlake:CreateResource", "healthlake:DeleteResource", "kms:ScheduleKeyDeletion:*"}
    assert not actions(doc) & forbidden
    (create,) = [s for s in statements(doc) if s["Sid"] == "HealthLakeCreateTagged"]
    assert create["Condition"] == {"StringEquals": {"aws:RequestTag/Project": "medsafety-intelligence"}}
    assert set(as_list(create["Action"])) == {"healthlake:CreateFHIRDatastore", "healthlake:TagResource"}  # TagResource: tag-on-create
    (passrole,) = [s for s in statements(doc) if s["Sid"] == "PassImportRoleToHealthLake"]
    assert passrole["Resource"].endswith("role/MedSafetyHealthLakeImportRole")
    assert passrole["Condition"] == {"StringEquals": {"iam:PassedToService": "healthlake.amazonaws.com"}}
    for s in statements(doc):
        if s["Sid"].startswith("IamRolesByName"):
            assert s["Resource"].endswith("role/MedSafetyHealthLake*")


def test_bucket_policy_denies_plain_http_and_non_kms_puts():
    st = {s["Sid"]: s for s in statements(load("bucket-policy.json.tpl"))}
    assert st["DenyInsecureTransport"]["Condition"] == {"Bool": {"aws:SecureTransport": "false"}}
    assert st["DenyNonKmsPuts"]["Condition"]["StringNotEqualsIfExists"] == {"s3:x-amz-server-side-encryption": "aws:kms"}


def test_kms_key_policy_delegates_to_iam_in_this_account_only():
    (s,) = statements(load("kms-key-policy.json.tpl"))
    assert s["Principal"] == {"AWS": f"arn:aws:iam::{ACCT}:root"}  # delegation; access is granted by the scoped identity policies


def test_deployer_datastore_scope_is_fhir_wildcard_in_this_account_and_region_with_project_tag():
    """User decision: `datastore/fhir/*` (never bare `datastore/*`), Project resource tag required on every datastore-scoped action (AWS documents aws:ResourceTag support for all of them)."""
    doc = load("deployer-policy.json.tpl")
    hl = [s for s in statements(doc) if any(a.startswith("healthlake:") for a in as_list(s["Action"]))]
    scoped = [s for s in hl if s["Resource"] != "*"]
    assert {s["Sid"] for s in scoped} == {"HealthLakeManageTaggedDatastore", "HealthLakeImportJobs"}
    for s in scoped:
        assert s["Resource"] == f"arn:aws:healthlake:us-east-1:{ACCT}:datastore/fhir/*"
    (managed,) = [s for s in scoped if s["Sid"] == "HealthLakeManageTaggedDatastore"]
    assert managed["Condition"] == {"StringEquals": {"aws:ResourceTag/Project": "medsafety-intelligence"}}
    assert set(managed["Action"]) == {"healthlake:DescribeFHIRDatastore", "healthlake:DeleteFHIRDatastore",
                                      "healthlake:TagResource", "healthlake:ListTagsForResource"}
    (jobs,) = [s for s in scoped if s["Sid"] == "HealthLakeImportJobs"]
    assert set(jobs["Action"]) == {"healthlake:StartFHIRImportJob", "healthlake:DescribeFHIRImportJob", "healthlake:ListFHIRImportJobs"}
    assert jobs["Condition"] == {"StringEquals": {"aws:ResourceTag/Project": "medsafety-intelligence"}}
    assert "datastore/*" not in json.dumps(doc)
    # the only unscoped HealthLake statements are create (RequestTag-gated) and list (AWS requires *)
    assert {s["Sid"] for s in hl if s["Resource"] == "*"} == {"HealthLakeCreateTagged", "HealthLakeList"}


def test_deployer_can_read_back_every_bucket_setting_it_writes():
    """The foundation step writes these settings; the deployer must be able to verify each one (GetLifecycleConfiguration was missed once)."""
    (buckets,) = [s for s in statements(load("deployer-policy.json.tpl")) if s["Sid"] == "S3Buckets"]
    acts = set(buckets["Action"])
    writes_to_reads = {"s3:PutBucketPublicAccessBlock": "s3:GetBucketPublicAccessBlock", "s3:PutBucketOwnershipControls": "s3:GetBucketOwnershipControls",
                       "s3:PutBucketVersioning": "s3:GetBucketVersioning", "s3:PutEncryptionConfiguration": "s3:GetEncryptionConfiguration",
                       "s3:PutBucketPolicy": "s3:GetBucketPolicy", "s3:PutLifecycleConfiguration": "s3:GetLifecycleConfiguration",
                       "s3:PutBucketTagging": "s3:GetBucketTagging"}
    for write, read in writes_to_reads.items():
        assert write in acts and read in acts, (write, read)
    assert set(buckets["Resource"]) == {f"arn:aws:s3:::medsafety-hl-import-{ACCT}-us-east-1", f"arn:aws:s3:::medsafety-hl-results-{ACCT}-us-east-1"}


def test_ram_permission_is_the_single_read_only_action_healthlake_create_needs():
    """CreateFHIRDatastore was denied on ram:GetResourceShareInvitations (approved addition; RAM cannot be resource-scoped)."""
    (ram,) = [s for s in statements(load("deployer-policy.json.tpl")) if s["Sid"] == "RamReadForHealthLakeCreate"]
    assert ram == {"Sid": "RamReadForHealthLakeCreate", "Effect": "Allow", "Action": "ram:GetResourceShareInvitations", "Resource": "*"}
    assert not [a for s in statements(load("deployer-policy.json.tpl")) for a in as_list(s["Action"]) if a.startswith("ram:") and a != ram["Action"]]


def test_import_role_lists_the_results_bucket_only_under_the_import_prefix():
    """HealthLake's pre-flight needs s3:ListBucket on the OUTPUT bucket (StartFHIRImportJob was rejected without it); approved, prefix-limited."""
    st = {s["Sid"]: s for s in statements(load("import-access.json.tpl"))}
    assert st["ListResultsPrefix"] == {"Sid": "ListResultsPrefix", "Effect": "Allow", "Action": "s3:ListBucket",
                                       "Resource": f"arn:aws:s3:::medsafety-hl-results-{ACCT}-us-east-1",
                                       "Condition": {"StringLike": {"s3:prefix": ["phase0-v1/*", "phase0-v1/"]}}}
    listers = [s for s in st.values() if "s3:ListBucket" in as_list(s["Action"])]
    assert {s["Sid"] for s in listers} == {"ListInputPrefix", "ListResultsPrefix"}
    assert all("Condition" in s for s in listers)  # never an unconditioned list
