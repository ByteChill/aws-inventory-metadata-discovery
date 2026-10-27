variable "env" {
  description = "Environment name (dev, prod)."
  type        = string
}

variable "image_tag" {
  description = "Tag of the inventory image in ECR."
  type        = string
}

variable "home_region" {
  description = "Region of the control plane."
  type        = string
  default     = "ap-east-1"
}

variable "global_home_region" {
  description = "Region whose cell scans global resources (IAM, Route 53)."
  type        = string
  default     = "us-east-1"
}

variable "regions" {
  description = <<-EOT
    Existing network per region (managed by the network team, only referenced here).
    endpoints: boto3 service name -> endpoint URL written into RegionCell.endpoints.
    Use "" for the region's own private DNS, or the endpoint-specific DNS
    (https://vpce-...) for services that other regions call over VPC peering.
  EOT
  type = map(object({
    vpc_id             = string
    cidr_block         = string
    private_subnet_ids = list(string)
    endpoints          = map(string)
  }))

  validation {
    condition     = length(setsubtract(keys(var.regions), ["ap-east-1", "eu-west-1", "eu-west-2", "us-east-1"])) == 0
    error_message = "Only ap-east-1, eu-west-1, eu-west-2 and us-east-1 have provider aliases."
  }
}

variable "adfs_idp_url" {
  description = "ADFS IdP-initiated sign-on URL for AWS."
  type        = string
}

variable "account_directory_function_name" {
  description = "Name of the existing aws-inventory-account-directory Lambda."
  type        = string
}

variable "worker_min_count" {
  type    = number
  default = 1
}

variable "worker_max_count" {
  type    = number
  default = 20
}

variable "api_desired_count" {
  type    = number
  default = 2
}

variable "db_instance_class" {
  type    = string
  default = "db.r7g.large"
}

variable "redis_node_type" {
  type    = string
  default = "cache.t4g.small"
}

variable "alarm_topic_arns" {
  description = "SNS topic per region for alarms (alarms must notify a topic in their own region)."
  type        = map(string)
  default     = {}
}

variable "log_retention_days" {
  type    = number
  default = 30
}

variable "broker_sts_region" {
  description = <<-EOT
    Region whose STS endpoint the broker calls for AssumeRoleWithSAML. Use a region enabled by
    default (the home region ap-east-1 is opt-in and fails for accounts that have not enabled it).
    The cell of that region must list an endpoint-specific "sts" endpoint reachable over peering.
  EOT
  type        = string
  default     = "us-east-1"
}

variable "schedules_enabled" {
  description = "Set false on the first deploy until migrations have run (see README)."
  type        = bool
  default     = true
}

variable "manage_ecr_replication" {
  description = "ECR replication is a registry-wide singleton: manage it from one stack per account."
  type        = bool
  default     = true
}

variable "adot_image" {
  description = "ADOT collector image; mirror it into this account's ECR (no internet access)."
  type        = string
  default     = "public.ecr.aws/aws-observability/aws-otel-collector:v0.43.1"
}

variable "api_tracing_enabled" {
  description = "Run the ADOT collector sidecar and enable OpenTelemetry in the API."
  type        = bool
  default     = false
}
