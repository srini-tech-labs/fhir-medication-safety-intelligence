{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":"*","Action":"execute-api:Invoke","Resource":"execute-api:/*","Condition":{"IpAddress":{"aws:SourceIp":"${ALLOWED_IP_CIDR}"}}}]}
