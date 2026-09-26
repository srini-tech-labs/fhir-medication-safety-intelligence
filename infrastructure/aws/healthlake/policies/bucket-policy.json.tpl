{"Version":"2012-10-17","Statement":[
 {"Sid":"DenyInsecureTransport","Effect":"Deny","Principal":"*","Action":"s3:*","Resource":["arn:aws:s3:::${BUCKET}","arn:aws:s3:::${BUCKET}/*"],"Condition":{"Bool":{"aws:SecureTransport":"false"}}},
 {"Sid":"DenyNonKmsPuts","Effect":"Deny","Principal":"*","Action":"s3:PutObject","Resource":"arn:aws:s3:::${BUCKET}/*","Condition":{"StringNotEqualsIfExists":{"s3:x-amz-server-side-encryption":"aws:kms"}}}]}
