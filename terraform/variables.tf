variable "env" {
  description = "Environment name (dev, prod)."
  type        = string
}

variable "component_versions" {
  description = "Image version (tag) of each component in ECR aws-inventory-discover/<component>; change one to release only that component."
  type = object({
    broker  = string
    planner = string
    api     = string
    worker  = string
  })
}

variable "primary_region" {
  description = "Region of the control plane (broker, planner, API, database, Redis)."
  type        = string
  default     = "ap-east-1"
}

variable "regions" {
  description = <<-EOT
    Scan regions and their existing network (managed by the network team, only referenced here).
    endpoints: endpoint-specific DNS (https://vpce-...) of this region's interface endpoints that
    other regions call over VPC peering (private DNS is not shared across peering):
      every region except the primary: sqs
      the primary region: lambda, secretsmanager
      us-east-1 (unless primary): sts, ec2
  EOT
  type = map(object({
    vpc_id             = string
    cidr_block         = string
    private_subnet_ids = list(string)
    endpoints          = optional(map(string), {})
  }))

  validation {
    condition     = contains(keys(var.regions), var.primary_region) && contains(keys(var.regions), "us-east-1")
    error_message = "regions must include the primary region and us-east-1 (global resources)."
  }
  validation {
    condition = alltrue([
      for region, cfg in var.regions : region == var.primary_region || contains(keys(cfg.endpoints), "sqs")
    ])
    error_message = "every region except the primary needs an sqs endpoint (the planner and API send to it)."
  }
  validation {
    condition = (
      length(var.regions) == 1 || length(setsubtract(["lambda", "secretsmanager"], keys(try(var.regions[var.primary_region].endpoints, {})))) == 0
    )
    error_message = "the primary region needs lambda and secretsmanager endpoints (workers in other regions call them)."
  }
  validation {
    condition = (
      var.primary_region == "us-east-1" || length(setsubtract(["sts", "ec2"], keys(try(var.regions["us-east-1"].endpoints, {})))) == 0
    )
    error_message = "us-east-1 needs sts (broker) and ec2 (DescribeRegions) endpoints."
  }
}

variable "lambda_architecture" {
  description = "Instruction set of the Lambda images (must match how they were built)."
  type        = string
  default     = "x86_64"

  validation {
    condition     = contains(["x86_64", "arm64"], var.lambda_architecture)
    error_message = "lambda_architecture must be x86_64 or arm64."
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

variable "deleted_retention_days" {
  description = "Days a soft-deleted resource is kept before maintenance purges it."
  type        = number
  default     = 180

  validation {
    condition     = var.deleted_retention_days >= 1
    error_message = "deleted_retention_days must be at least 1."
  }
}

variable "log_retention_days" {
  type    = number
  default = 30
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
