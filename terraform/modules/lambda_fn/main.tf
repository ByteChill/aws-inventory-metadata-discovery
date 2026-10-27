terraform {
  required_providers {
    aws = { source = "hashicorp/aws" }
  }
}

variable "name" { type = string }
variable "image_uri" { type = string }
variable "command" { type = list(string) }
variable "role_arn" { type = string }
variable "environment" { type = map(string) }
variable "timeout" {
  type    = number
  default = 60
}
variable "memory_size" {
  type    = number
  default = 512
}
variable "subnet_ids" {
  type    = list(string)
  default = []
}
variable "security_group_ids" {
  type    = list(string)
  default = []
}
variable "log_retention_days" {
  type    = number
  default = 30
}
variable "reserved_concurrency" {
  type    = number
  default = -1
}

resource "aws_cloudwatch_log_group" "this" {
  name              = "/aws/lambda/${var.name}"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "this" {
  function_name                  = var.name
  package_type                   = "Image"
  image_uri                      = var.image_uri
  role                           = var.role_arn
  architectures                  = ["arm64"]
  timeout                        = var.timeout
  memory_size                    = var.memory_size
  reserved_concurrent_executions = var.reserved_concurrency

  image_config {
    command = var.command
  }

  environment {
    variables = var.environment
  }

  dynamic "vpc_config" {
    for_each = length(var.subnet_ids) > 0 ? [1] : []
    content {
      subnet_ids         = var.subnet_ids
      security_group_ids = var.security_group_ids
    }
  }

  tracing_config {
    mode = "Active"
  }

  logging_config {
    log_format = "JSON"
    log_group  = aws_cloudwatch_log_group.this.name
  }
}

output "arn" { value = aws_lambda_function.this.arn }
output "name" { value = aws_lambda_function.this.function_name }
output "log_group_name" { value = aws_cloudwatch_log_group.this.name }
