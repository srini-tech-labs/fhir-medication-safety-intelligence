{"Version":"2012-10-17","Statement":[
 {"Sid":"InvokeNova2LiteUsProfile","Effect":"Allow","Action":"bedrock:InvokeModel","Resource":"${PROFILE_ARN}"},
 {"Sid":"InvokeNova2LiteFoundationModelOnlyViaThatProfile","Effect":"Allow","Action":"bedrock:InvokeModel","Resource":${FOUNDATION_MODEL_ARNS_JSON},"Condition":{"StringLike":{"bedrock:InferenceProfileArn":"${PROFILE_ARN}"}}},
 {"Sid":"DescribeNova2LiteUsProfile","Effect":"Allow","Action":"bedrock:GetInferenceProfile","Resource":"${PROFILE_ARN}"}]}
