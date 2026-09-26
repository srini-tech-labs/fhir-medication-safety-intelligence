{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"healthlake.amazonaws.com"},"Action":"sts:AssumeRole",
 "Condition":{"StringEquals":{"aws:SourceAccount":"${ACCT}"},"ArnEquals":{"aws:SourceArn":"arn:aws:healthlake:us-east-1:${ACCT}:datastore/fhir/${DS}"}}}]}
