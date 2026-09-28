"""Phase 4 IAM / API policy templates against the approved plan (Revision 2). No AWS access."""
from __future__ import annotations

import json
import re
import sys

import pytest

from app.config import REPO_ROOT

API = REPO_ROOT / "infrastructure" / "aws" / "api"
HL = REPO_ROOT / "infrastructure" / "aws" / "healthlake"
sys.path.insert(0, str(HL))
from render_policy import render  # noqa: E402

ACCT, DS = "123456789012", "00000000000000000000000000000002"
KEY = f"arn:aws:kms:us-east-1:{ACCT}:key/00000000-0000-0000-0000-000000000000"
PROFILE = f"arn:aws:bedrock:us-east-1:{ACCT}:inference-profile/us.amazon.nova-2-lite-v1:0"
FM = [f"arn:aws:bedrock:{r}::foundation-model/amazon.nova-2-lite-v1:0" for r in ("us-east-1", "us-east-2", "us-west-2")]
ENV = {"ACCT": ACCT, "DS": DS, "KEY_ARN": KEY, "TABLE": "medsafety-app-state", "LOG_GROUP": "/aws/lambda/medsafety-api",
       "PROFILE_ARN": PROFILE, "FOUNDATION_MODEL_ARNS_JSON": json.dumps(FM), "ALLOWED_IP_CIDR": "203.0.113.7/32",
       "OPENAI_SECRET_ARN": f"arn:aws:secretsmanager:us-east-1:{ACCT}:secret:medsafety/openai-api-key-abc123"}


def load(name: str) -> dict:
    return json.loads(render((API / "policies" / name).read_text("utf-8"), ENV))


def stmts(doc):
    return doc["Statement"]


def as_list(x):
    return [x] if isinstance(x, str) else x


def actions(doc):
    return {a for s in stmts(doc) for a in as_list(s["Action"])}


TEMPLATES = sorted(p.name for p in (API / "policies").glob("*.tpl"))


@pytest.mark.parametrize("name", TEMPLATES)
def test_every_template_renders_to_json_without_leftover_placeholders(name):
    text = render((API / "policies" / name).read_text("utf-8"), ENV)
    assert "${" not in text and json.loads(text)


def test_lambda_trust_is_the_lambda_service_only():
    (s,) = stmts(json.loads((API / "policies" / "lambda-trust.json").read_text()))
    assert s["Principal"] == {"Service": "lambda.amazonaws.com"} and s["Action"] == "sts:AssumeRole" and "Condition" not in s


# ---- Lambda runtime role ---------------------------------------------------------------------------------------
def test_lambda_role_has_exactly_the_approved_base_permissions():
    """healthlake:GetCapabilities (found missing during the live Phase 5 /v1/ready rollout: the FHIR capability
    statement -- GET metadata, what the readiness clinicalStore probe calls -- 403'd without it, since it is a
    distinct action from ReadResource/SearchWithGet) and healthlake:UpdateResource (proposed, NOT YET applied to
    AWS: least-privilege grant for the explicit persist_to_fhir() write-back path -- see docs/adr and the
    write-back deployment plan) make up the deployed role's exact base permission set. UpdateResource ONLY, not
    CreateResource: AWS HealthLake documents FHIR PUT as the "update" interaction, which creates the initial
    version itself when the id doesn't yet exist -- CreateResource is deliberately withheld unless a live PUT
    against a not-yet-existing id actually demonstrates it's required (see tests/live_aws/test_healthlake_persist_live.py)."""
    doc = load("lambda-access.json.tpl")
    assert actions(doc) == {"healthlake:ReadResource", "healthlake:SearchWithGet", "healthlake:GetCapabilities",
                            "healthlake:UpdateResource", "kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey",
                            "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query", "logs:CreateLogStream", "logs:PutLogEvents"}
    by = {s["Sid"]: s for s in stmts(doc)}
    assert by["FhirRead"]["Resource"] == f"arn:aws:healthlake:us-east-1:{ACCT}:datastore/fhir/{DS}"
    assert by["FhirPersistDeterministicOutputs"]["Resource"] == f"arn:aws:healthlake:us-east-1:{ACCT}:datastore/fhir/{DS}"
    assert by["AppState"]["Resource"] == f"arn:aws:dynamodb:us-east-1:{ACCT}:table/medsafety-app-state"
    assert by["Logs"]["Resource"] == f"arn:aws:logs:us-east-1:{ACCT}:log-group:/aws/lambda/medsafety-api:*"


def test_lambda_role_grants_every_healthlake_action_the_offline_fake_requires_of_the_app_role():
    """Structural regression: tests/support/fake_healthlake.py's APP_ACTIONS (what every local/offline test
    exercises against, including tests/live_aws) already modelled GetCapabilities as required -- but the real
    deployed lambda-access.json.tpl had drifted and never granted it, so /v1/ready's clinicalStore probe (GET
    metadata) 403'd against live AWS while every offline test stayed green. Ties the two together so the next
    such drift fails a fast local test instead of only surfacing on a live deployment. SearchWithPost and
    CreateResource remain genuinely unused: the app only ever issues GET-based search, and the fake's own PUT
    handler classifies every PUT as "UpdateResource" regardless of whether the id already existed (matching
    HealthLake's documented behavior that PUT is the FHIR "update" interaction) -- neither is required."""
    sys.path.insert(0, str(REPO_ROOT / "tests" / "support"))
    from fake_healthlake import APP_ACTIONS  # noqa: E402

    deployed = {a.removeprefix("healthlake:") for a in actions(load("lambda-access.json.tpl")) if a.startswith("healthlake:")}
    used_by_this_app = APP_ACTIONS - {"SearchWithPost", "CreateResource"}
    assert used_by_this_app <= deployed, f"missing: {sorted(used_by_this_app - deployed)}"


def test_lambda_role_grants_update_only_for_fhir_persist_never_create_wildcard_or_history():
    """The persist_to_fhir() write-back path is proposed to get exactly healthlake:UpdateResource -- not
    CreateResource, which is deliberately withheld until a live PUT test demonstrates it's actually needed (AWS
    HealthLake documents PUT as the FHIR "update" interaction, which creates the initial version itself when
    the id is new). Everything else stays forbidden: no delete, no history/version reads, no bundle processing,
    no wildcard, scoped to exactly the one datastore ARN, not `*`."""
    doc = load("lambda-access.json.tpl")
    acts = actions(doc)
    assert "healthlake:UpdateResource" in acts
    assert "healthlake:CreateResource" not in acts  # withheld pending live-PUT evidence, not just "also fine to have"
    for forbidden in ("healthlake:DeleteResource", "healthlake:GetHistoryByResourceId", "healthlake:VersionReadResource",
                      "healthlake:ProcessBundle", "healthlake:SearchWithPost", "dynamodb:Scan", "dynamodb:DeleteItem"):
        assert forbidden not in acts
    by = {s["Sid"]: s for s in stmts(doc)}
    assert by["FhirPersistDeterministicOutputs"]["Action"] == "healthlake:UpdateResource"
    assert by["FhirPersistDeterministicOutputs"]["Resource"] == f"arn:aws:healthlake:us-east-1:{ACCT}:datastore/fhir/{DS}"
    assert not any(a in ("*",) or a.endswith(":*") for a in acts)
    assert not any(a.startswith(("bedrock", "bedrock-mantle")) for a in acts)  # Bedrock is Gate B, a separate inline policy


def test_dynamodb_uses_default_encryption_so_the_role_has_no_dynamodb_kms_permissions():
    """User decision (Revision 2): the only KMS statement is the HealthLake one, limited to that service."""
    kms = [s for s in stmts(load("lambda-access.json.tpl")) if any(a.startswith("kms:") for a in as_list(s["Action"]))]
    assert [s["Sid"] for s in kms] == ["CmkViaHealthLake"]
    assert kms[0]["Condition"] == {"StringEquals": {"kms:ViaService": "healthlake.us-east-1.amazonaws.com"}}
    assert "dynamodb.us-east-1.amazonaws.com" not in json.dumps(load("lambda-access.json.tpl"))


def test_lambda_actions_are_a_subset_of_the_phase3_app_role():
    app = json.loads(render((HL / "policies" / "app-access.json.tpl").read_text("utf-8"), {**ENV, "IMPORT_BUCKET": "i", "RESULTS_BUCKET": "r", "BUCKET": "b", "PRINCIPAL_ARN": "x"}))
    hl = {a for a in actions(load("lambda-access.json.tpl")) if a.startswith(("healthlake:", "kms:"))}
    assert hl <= {a for s in stmts(app) for a in as_list(s["Action"])}


# ---- Gate B: Bedrock statements (drafted, applied only after approval) ----------------------------------------------
def test_bedrock_statements_are_exactly_the_approved_nova_profile_permissions():
    """Revision 3 + the two corrections: InvokeModel on the approved profile and its three member models (only via that profile), plus
    GetInferenceProfile on that one profile ARN. Nothing else: no streaming, no wildcard, no Marketplace, no Mantle."""
    doc = load("lambda-bedrock.json.tpl")
    assert actions(doc) == {"bedrock:InvokeModel", "bedrock:GetInferenceProfile"}
    by = {s["Sid"]: s for s in stmts(doc)}
    assert set(by) == {"InvokeNova2LiteUsProfile", "InvokeNova2LiteFoundationModelOnlyViaThatProfile", "DescribeNova2LiteUsProfile"}
    assert by["InvokeNova2LiteUsProfile"] == {"Sid": "InvokeNova2LiteUsProfile", "Effect": "Allow", "Action": "bedrock:InvokeModel", "Resource": PROFILE}
    fm = by["InvokeNova2LiteFoundationModelOnlyViaThatProfile"]
    assert fm["Action"] == "bedrock:InvokeModel" and fm["Resource"] == FM and len(fm["Resource"]) == 3
    assert fm["Condition"] == {"StringLike": {"bedrock:InferenceProfileArn": PROFILE}}
    assert by["DescribeNova2LiteUsProfile"] == {"Sid": "DescribeNova2LiteUsProfile", "Effect": "Allow", "Action": "bedrock:GetInferenceProfile", "Resource": PROFILE}
    text = json.dumps(doc)
    assert "*" not in text.replace("InvokeModel", "") and "mantle" not in text.lower() and "marketplace" not in text.lower()
    assert "WithResponseStream" not in text and "anthropic" not in text and "global." not in text


def test_the_paused_anthropic_path_has_no_permissions_anywhere_in_the_phase4_policies():
    for name in TEMPLATES:
        assert "anthropic" not in json.dumps(load(name)).lower(), name


# ---- Gate D: OpenAI Secrets Manager read (drafted, applied only after approval) ---------------------------------
def test_openai_secret_policy_grants_only_getsecretvalue_scoped_to_the_one_secret_arn():
    """docs/adr/0011: least privilege -- read exactly one secret, nothing else (no List/Put/Delete, no wildcard)."""
    doc = load("lambda-secrets-openai.json.tpl")
    assert actions(doc) == {"secretsmanager:GetSecretValue"}
    (s,) = stmts(doc)
    assert s == {"Sid": "ReadOpenAiApiKeySecretOnly", "Effect": "Allow", "Action": "secretsmanager:GetSecretValue", "Resource": ENV["OPENAI_SECRET_ARN"]}
    text = json.dumps(doc)
    assert "*" not in text and "kms" not in text.lower()  # default Secrets Manager encryption key; no separate KMS grant needed


# ---- API resource policy -----------------------------------------------------------------------------------------
def test_api_resource_policy_allows_invoke_only_from_one_ip():
    (s,) = stmts(load("api-resource-policy.json.tpl"))
    assert s["Action"] == "execute-api:Invoke" and s["Resource"] == "execute-api:/*"
    assert s["Condition"] == {"IpAddress": {"aws:SourceIp": "203.0.113.7/32"}}


# ---- deployer policy (new, additive) ---------------------------------------------------------------------------
def test_phase4_deployer_policy_is_scoped_and_has_no_invoke_or_kms_rights():
    doc = load("deployer-policy-phase4.json.tpl")
    acts = actions(doc)
    assert not any(a in ("*",) or a.endswith(":*") for a in acts)
    assert not any(a.startswith("kms:") for a in acts), "no KMS additions (DynamoDB uses default encryption)"
    assert not any(a.startswith(("bedrock:Invoke", "bedrock-mantle")) for a in acts), "the deployer gets no Bedrock invoke right"
    assert {a for a in acts if a.startswith("bedrock:")} == {"bedrock:GetFoundationModelAvailability", "bedrock:GetFoundationModel", "bedrock:ListFoundationModels",
                                                             "bedrock:GetInferenceProfile", "bedrock:ListInferenceProfiles", "bedrock:GetUseCaseForModelAccess",
                                                             "bedrock:ListFoundationModelAgreementOffers"}
    for forbidden in ("iam:AttachRolePolicy", "iam:CreatePolicy", "iam:CreateUser", "apigateway:SetWebACL", "lambda:InvokeFunctionUrl", "healthlake:CreateResource"):
        assert forbidden not in acts
    star = {s["Sid"] for s in stmts(doc) if "*" in as_list(s["Resource"])}
    assert star == {"LogsDescribeGroups", "BedrockReadOnlyForPreflight"}  # both read-only and cannot be resource-scoped


def test_phase4_deployer_policy_can_create_the_openai_secret_but_nothing_else_in_secrets_manager():
    """The deployer role needs to CREATE the secret (Gate C); reading it at runtime is a completely separate,
    narrower grant on MedSafetyApiLambdaRole (Gate D, lambda-secrets-openai.json.tpl). Scoped to exactly the
    medsafety/openai-api-key-* name prefix (Secrets Manager appends a random 6-char suffix at creation time, so
    the exact post-creation ARN can't be known up front) -- never a bare "*" and never GetSecretValue here."""
    doc = load("deployer-policy-phase4.json.tpl")
    by = {s["Sid"]: s for s in stmts(doc)}
    s = by["OpenAiSecretDeploy"]
    assert set(as_list(s["Action"])) == {"secretsmanager:CreateSecret", "secretsmanager:DescribeSecret", "secretsmanager:TagResource"}
    assert s["Resource"] == f"arn:aws:secretsmanager:us-east-1:{ACCT}:secret:medsafety/openai-api-key-*"
    assert "secretsmanager:GetSecretValue" not in actions(doc)  # runtime read access is Gate D's grant, not the deployer's
    assert not any(a == "secretsmanager:*" or (a.startswith("secretsmanager:") and "Delete" in a) for a in actions(doc))


def test_phase4_deployer_scoping_by_name():
    by = {s["Sid"]: s for s in stmts(load("deployer-policy-phase4.json.tpl"))}
    assert by["LambdaFunction"]["Resource"] == f"arn:aws:lambda:us-east-1:{ACCT}:function:medsafety-api"
    assert by["IamApiRoleByName"]["Resource"] == f"arn:aws:iam::{ACCT}:role/MedSafetyApi*"
    assert by["PassApiRoleToLambda"]["Resource"].endswith("role/MedSafetyApiLambdaRole")
    assert by["PassApiRoleToLambda"]["Condition"] == {"StringEquals": {"iam:PassedToService": "lambda.amazonaws.com"}}
    assert by["DynamoDbAppStateTable"]["Resource"] == f"arn:aws:dynamodb:us-east-1:{ACCT}:table/medsafety-app-state"
    assert set(by["ApiGatewayRestApis"]["Resource"]) == {"arn:aws:apigateway:us-east-1::/restapis", "arn:aws:apigateway:us-east-1::/restapis/*", "arn:aws:apigateway:us-east-1::/tags/*"}
    assert "apigateway:UpdateRestApiPolicy" in by["ApiGatewayRestApis"]["Action"]
    assert not any("/account" in r for s in stmts(load("deployer-policy-phase4.json.tpl")) for r in as_list(s["Resource"]))  # no account-level API Gateway settings
    assert all(f"/aws/lambda/medsafety-api" in r for r in by["LogsLambdaGroup"]["Resource"])


def test_phase4_deployer_policy_fits_a_managed_policy():
    text = render((API / "policies" / "deployer-policy-phase4.json.tpl").read_text("utf-8"), ENV)
    assert len(re.sub(r"\s", "", text)) < 6144


def test_existing_phase3_policy_templates_are_untouched_by_phase4():
    """Phase 4 adds files; it must not edit the Phase 3 deployer policy (the current IAM configuration stays as it is)."""
    text = (HL / "policies" / "deployer-policy.json.tpl").read_text()
    assert "lambda" not in text.lower() and "apigateway" not in text and "dynamodb" not in text
