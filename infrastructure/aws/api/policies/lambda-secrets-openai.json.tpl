{"Version":"2012-10-17","Statement":[
 {"Sid":"ReadOpenAiApiKeySecretOnly","Effect":"Allow","Action":"secretsmanager:GetSecretValue","Resource":"${OPENAI_SECRET_ARN}"}]}
