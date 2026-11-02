env                             = "prod"
adfs_idp_url                    = "https://adfs.corp.example/adfs/ls/IdpInitiatedSignOn.aspx?loginToRp=urn:amazon:webservices"
account_directory_function_name = "aws-inventory-account-directory"
worker_min_count                = 1
worker_max_count                = 20
api_desired_count               = 2
db_instance_class               = "db.r7g.large"

# Released by changing a version here (PR -> merge -> tag -> terraform apply); each version
# is an image tag in ECR aws-inventory-discover/<component> (deploy/versions.json).
component_versions = {
  broker  = "1.0.0"
  planner = "1.0.0"
  api     = "1.0.0"
  worker  = "1.0.0"
}

# Scan regions and their existing network (owned by the network team).
# endpoints: endpoint-specific DNS only for services other regions call over peering;
# same-region calls use the VPC's private DNS and need no entry.
regions = {
  "ap-east-1" = {
    vpc_id             = "vpc-0aaaaaaaaaaaaaaa1"
    cidr_block         = "10.10.0.0/16"
    private_subnet_ids = ["subnet-0aaaaaaaaaaaaaaa1", "subnet-0aaaaaaaaaaaaaaa2"]
    endpoints = {
      lambda         = "https://vpce-0aaaaaaaaaaaaaaa1-lambda.lambda.ap-east-1.vpce.amazonaws.com"                 # workers -> broker
      secretsmanager = "https://vpce-0aaaaaaaaaaaaaaa1-secretsmanager.secretsmanager.ap-east-1.vpce.amazonaws.com" # workers -> database secret
    }
  }
  "eu-west-1" = {
    vpc_id             = "vpc-0bbbbbbbbbbbbbbb1"
    cidr_block         = "10.20.0.0/16"
    private_subnet_ids = ["subnet-0bbbbbbbbbbbbbbb1", "subnet-0bbbbbbbbbbbbbbb2"]
    endpoints = {
      sqs = "https://vpce-0bbbbbbbbbbbbbbb1-sqs.sqs.eu-west-1.vpce.amazonaws.com" # planner / API -> this region's queues
    }
  }
  "eu-west-2" = {
    vpc_id             = "vpc-0ccccccccccccccc1"
    cidr_block         = "10.30.0.0/16"
    private_subnet_ids = ["subnet-0ccccccccccccccc1", "subnet-0ccccccccccccccc2"]
    endpoints = {
      sqs = "https://vpce-0ccccccccccccccc1-sqs.sqs.eu-west-2.vpce.amazonaws.com" # planner / API -> this region's queues
    }
  }
  "us-east-1" = {
    vpc_id             = "vpc-0ddddddddddddddd1"
    cidr_block         = "10.40.0.0/16"
    private_subnet_ids = ["subnet-0ddddddddddddddd1", "subnet-0ddddddddddddddd2"]
    endpoints = {
      sqs = "https://vpce-0ddddddddddddddd1-sqs.sqs.us-east-1.vpce.amazonaws.com" # planner / API -> this region's queues
      sts = "https://vpce-0ddddddddddddddd1-sts.sts.us-east-1.vpce.amazonaws.com" # broker AssumeRoleWithSAML
      ec2 = "https://vpce-0ddddddddddddddd1-ec2.ec2.us-east-1.vpce.amazonaws.com" # DescribeRegions
    }
  }
}
