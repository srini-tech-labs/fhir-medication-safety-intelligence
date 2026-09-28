{"Version":"2012-10-17","Statement":[
 {"Sid":"FhirDataPlane","Effect":"Allow","Action":["healthlake:ReadResource","healthlake:SearchWithGet","healthlake:SearchWithPost","healthlake:GetCapabilities","healthlake:CreateResource","healthlake:UpdateResource"],"Resource":"arn:aws:healthlake:us-east-1:${ACCT}:datastore/fhir/${DS}"},
 {"Sid":"Describe","Effect":"Allow","Action":"healthlake:DescribeFHIRDatastore","Resource":"arn:aws:healthlake:us-east-1:${ACCT}:datastore/fhir/${DS}"},
 {"Sid":"CmkViaHealthLake","Effect":"Allow","Action":["kms:Decrypt","kms:GenerateDataKey","kms:DescribeKey"],"Resource":"${KEY_ARN}","Condition":{"StringEquals":{"kms:ViaService":"healthlake.us-east-1.amazonaws.com"}}}]}
