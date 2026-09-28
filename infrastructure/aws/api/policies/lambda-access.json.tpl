{"Version":"2012-10-17","Statement":[
 {"Sid":"FhirRead","Effect":"Allow","Action":["healthlake:ReadResource","healthlake:SearchWithGet","healthlake:GetCapabilities"],"Resource":"arn:aws:healthlake:us-east-1:${ACCT}:datastore/fhir/${DS}"},
 {"Sid":"FhirPersistDeterministicOutputs","Effect":"Allow","Action":"healthlake:UpdateResource","Resource":"arn:aws:healthlake:us-east-1:${ACCT}:datastore/fhir/${DS}"},
 {"Sid":"CmkViaHealthLake","Effect":"Allow","Action":["kms:Decrypt","kms:GenerateDataKey","kms:DescribeKey"],"Resource":"${KEY_ARN}","Condition":{"StringEquals":{"kms:ViaService":"healthlake.us-east-1.amazonaws.com"}}},
 {"Sid":"AppState","Effect":"Allow","Action":["dynamodb:GetItem","dynamodb:PutItem","dynamodb:UpdateItem","dynamodb:Query"],"Resource":"arn:aws:dynamodb:us-east-1:${ACCT}:table/${TABLE}"},
 {"Sid":"Logs","Effect":"Allow","Action":["logs:CreateLogStream","logs:PutLogEvents"],"Resource":"arn:aws:logs:us-east-1:${ACCT}:log-group:${LOG_GROUP}:*"}]}
