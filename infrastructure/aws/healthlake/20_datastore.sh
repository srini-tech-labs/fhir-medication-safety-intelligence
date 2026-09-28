#!/usr/bin/env bash
# Creates the HealthLake R4 datastore (AWS_AUTH/SigV4, customer-managed KMS) and waits for ACTIVE. Billing starts here ($0.27/h).
# Analytics is created PAUSED (user decision): DISABLED is rejected at creation (ENABLED|PAUSED only), and PAUSED keeps the option to enable it later.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
load_identity; require_state KEY_ARN

DS_ID="$(aws_ro healthlake list-fhir-datastores --query "DatastorePropertiesList[?DatastoreName=='$DS_NAME' && DatastoreStatus!='DELETED'].DatastoreId" --output text)"
if [ -z "$DS_ID" ]; then
  log "creating datastore $DS_NAME (billing starts now, ~\$0.27/hour until deleted)"
  mapfile -t TAGS < <(tags_cli)
  DS_ID="$(aws_mut healthlake create-fhir-datastore --datastore-name "$DS_NAME" --datastore-type-version R4 \
      --sse-configuration "{\"KmsEncryptionConfig\":{\"CmkType\":\"CUSTOMER_MANAGED_KMS_KEY\",\"KmsKeyId\":\"$KEY_ARN\"}}" \
      --analytics-configuration Status=PAUSED \
      --tags "${TAGS[@]}" --client-token "$DS_CLIENT_TOKEN" --query DatastoreId --output text)"
else log "reusing datastore $DS_ID"; fi
[ "${DRY_RUN:-0}" = "1" ] && exit 0
save_state DS_ID "$DS_ID"; grep -q '^DS_CREATED_AT=' "$STATE_FILE" || save_state DS_CREATED_AT "$(date -u +%FT%TZ)"

until [ "$(aws_ro healthlake describe-fhir-datastore --datastore-id "$DS_ID" --query DatastoreProperties.DatastoreStatus --output text)" = ACTIVE ]; do
  st="$(aws_ro healthlake describe-fhir-datastore --datastore-id "$DS_ID" --query DatastoreProperties.DatastoreStatus --output text)"
  [ "$st" = CREATE_FAILED ] && die "datastore creation failed (SCP / KMS grant?): $(aws_ro healthlake describe-fhir-datastore --datastore-id "$DS_ID" --query DatastoreProperties.ErrorCause --output json)"
  log "datastore status: $st"; sleep 30
done
aws_ro healthlake describe-fhir-datastore --datastore-id "$DS_ID" --output json > "$STATE_DIR/datastore.json"
save_state DS_ARN "$(python3 -c "import json;print(json.load(open('$STATE_DIR/datastore.json'))['DatastoreProperties']['DatastoreArn'])")"
save_state DS_ENDPOINT "$(python3 -c "import json;print(json.load(open('$STATE_DIR/datastore.json'))['DatastoreProperties']['DatastoreEndpoint'])")"
python3 - <<PY
import json; p=json.load(open("$STATE_DIR/datastore.json"))["DatastoreProperties"]
auth=(p.get("IdentityProviderConfiguration") or {}).get("AuthorizationStrategy", "AWS_AUTH")
assert auth == "AWS_AUTH", f"unexpected authorization strategy {auth}"
kms=p["SseConfiguration"]["KmsEncryptionConfig"]; assert kms["CmkType"]=="CUSTOMER_MANAGED_KMS_KEY", kms
analytics=(p.get("AnalyticsConfiguration") or {}).get("Status")
assert p["DatastoreTypeVersion"] == "R4", p["DatastoreTypeVersion"]
assert analytics == "PAUSED", f"AnalyticsConfiguration.Status is {analytics!r}, expected PAUSED"
print("datastore ACTIVE:", p["DatastoreId"], p["DatastoreTypeVersion"], "auth="+auth, "analytics="+analytics, "cmk="+kms.get("KmsKeyId",""))
PY
