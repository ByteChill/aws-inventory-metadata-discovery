env                             = "prod"
image_tag                       = "REPLACE-with-release-tag" # tags are immutable
adfs_idp_url                    = "https://adfs.corp.example/adfs/ls/IdpInitiatedSignOn.aspx?loginToRp=urn:amazon:webservices"
account_directory_function_name = "aws-inventory-account-directory"
worker_min_count                = 1
worker_max_count                = 20
api_desired_count               = 2
db_instance_class               = "db.r7g.large"

# Existing network (owned by the network team). Endpoints: "" = this VPC's private DNS;
# "https://vpce-..." = endpoint-specific DNS, required where other regions call over peering.
regions = {
  "ap-east-1" = {
    vpc_id             = "vpc-0aaaaaaaaaaaaaaa1"
    cidr_block         = "10.10.0.0/16"
    private_subnet_ids = ["subnet-0aaaaaaaaaaaaaaa1", "subnet-0aaaaaaaaaaaaaaa2"]
    endpoints = {
      ec2        = ""
      ecs        = ""
      s3         = ""
      sqs        = "https://vpce-0aaaaaaaaaaaaaaa1-sqs.sqs.ap-east-1.vpce.amazonaws.com"
      lambda     = "https://vpce-0aaaaaaaaaaaaaaa1-lambda.lambda.ap-east-1.vpce.amazonaws.com"
      sts        = ""
      kms        = ""
      monitoring = ""
      # read by workers in every region (database secret) -> endpoint-specific DNS
      secretsmanager = "https://vpce-0aaaaaaaaaaaaaaa1-secretsmanager.secretsmanager.ap-east-1.vpce.amazonaws.com"
    }
  }
  "eu-west-1" = {
    vpc_id             = "vpc-0bbbbbbbbbbbbbbb1"
    cidr_block         = "10.20.0.0/16"
    private_subnet_ids = ["subnet-0bbbbbbbbbbbbbbb1", "subnet-0bbbbbbbbbbbbbbb2"]
    endpoints = {
      ec2        = "https://vpce-0bbbbbbbbbbbbbbb1-ec2.ec2.eu-west-1.vpce.amazonaws.com"
      ecs        = "https://vpce-0bbbbbbbbbbbbbbb1-ecs.ecs.eu-west-1.vpce.amazonaws.com"
      s3         = ""
      sqs        = "https://vpce-0bbbbbbbbbbbbbbb1-sqs.sqs.eu-west-1.vpce.amazonaws.com"
      monitoring = ""
    }
  }
  "eu-west-2" = {
    vpc_id             = "vpc-0ccccccccccccccc1"
    cidr_block         = "10.30.0.0/16"
    private_subnet_ids = ["subnet-0ccccccccccccccc1", "subnet-0ccccccccccccccc2"]
    endpoints = {
      ec2        = "https://vpce-0ccccccccccccccc1-ec2.ec2.eu-west-2.vpce.amazonaws.com"
      ecs        = "https://vpce-0ccccccccccccccc1-ecs.ecs.eu-west-2.vpce.amazonaws.com"
      s3         = ""
      sqs        = "https://vpce-0ccccccccccccccc1-sqs.sqs.eu-west-2.vpce.amazonaws.com"
      monitoring = ""
    }
  }
  "us-east-1" = {
    vpc_id             = "vpc-0ddddddddddddddd1"
    cidr_block         = "10.40.0.0/16"
    private_subnet_ids = ["subnet-0ddddddddddddddd1", "subnet-0ddddddddddddddd2"]
    endpoints = {
      ec2        = "https://vpce-0ddddddddddddddd1-ec2.ec2.us-east-1.vpce.amazonaws.com"
      ecs        = "https://vpce-0ddddddddddddddd1-ecs.ecs.us-east-1.vpce.amazonaws.com"
      s3         = ""
      sqs        = "https://vpce-0ddddddddddddddd1-sqs.sqs.us-east-1.vpce.amazonaws.com"
      monitoring = ""
      # the broker (ap-east-1) calls STS here: us-east-1 is enabled for every account
      sts = "https://vpce-0ddddddddddddddd1-sts.sts.us-east-1.vpce.amazonaws.com"
      # iam and route53 are only listed once interface endpoints exist for them;
      # until then the global collectors are skipped (skipped_no_endpoint).
    }
  }
}
