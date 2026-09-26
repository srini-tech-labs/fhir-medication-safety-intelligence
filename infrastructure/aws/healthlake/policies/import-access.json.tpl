{"Version":"2012-10-17","Statement":[
 {"Sid":"InputBucketMeta","Effect":"Allow","Action":["s3:GetBucketPublicAccessBlock","s3:GetEncryptionConfiguration","s3:GetBucketLocation"],"Resource":"arn:aws:s3:::${IMPORT_BUCKET}"},
 {"Sid":"ListInputPrefix","Effect":"Allow","Action":"s3:ListBucket","Resource":"arn:aws:s3:::${IMPORT_BUCKET}","Condition":{"StringLike":{"s3:prefix":["phase0-v1/*","phase0-v1/"]}}},
 {"Sid":"ReadInput","Effect":"Allow","Action":"s3:GetObject","Resource":"arn:aws:s3:::${IMPORT_BUCKET}/phase0-v1/*"},
 {"Sid":"ResultsBucketMeta","Effect":"Allow","Action":["s3:GetBucketPublicAccessBlock","s3:GetEncryptionConfiguration","s3:GetBucketLocation"],"Resource":"arn:aws:s3:::${RESULTS_BUCKET}"},
 {"Sid":"ListResultsPrefix","Effect":"Allow","Action":"s3:ListBucket","Resource":"arn:aws:s3:::${RESULTS_BUCKET}","Condition":{"StringLike":{"s3:prefix":["phase0-v1/*","phase0-v1/"]}}},
 {"Sid":"WriteResults","Effect":"Allow","Action":"s3:PutObject","Resource":"arn:aws:s3:::${RESULTS_BUCKET}/phase0-v1/*"},
 {"Sid":"UseCmk","Effect":"Allow","Action":["kms:DescribeKey","kms:Decrypt","kms:GenerateDataKey*"],"Resource":"${KEY_ARN}"}]}
