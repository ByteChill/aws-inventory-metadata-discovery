# One region cell: scheduled/ondemand queues (+DLQs) and the ECS worker service.
# Every regional resource sets `region` (AWS provider 6.x), so one provider serves all cells.

terraform {
  required_providers {
    aws = { source = "hashicorp/aws" }
  }
}

variable "name_prefix" { type = string }
variable "region" { type = string }
variable "vpc_id" { type = string }
variable "subnet_ids" { type = list(string) }
variable "image_uri" { type = string }
variable "environment" { type = map(string) }
variable "secrets" {
  description = "ECS secrets: env var name -> Secrets Manager ARN."
  type        = map(string)
  default     = {}
}
variable "task_role_policy_json" { type = string }
variable "min_count" { type = number }
variable "max_count" { type = number }
variable "log_retention_days" { type = number }
variable "alarm_topic_arn" {
  type    = string
  default = null
}
variable "cpu" {
  type    = number
  default = 512
}
variable "memory" {
  type    = number
  default = 1024
}

locals {
  queues = toset(["scheduled", "ondemand"])
}

# --------------------------------------------------------------------------- queues

resource "aws_sqs_queue" "dlq" {
  region                    = var.region
  for_each                  = local.queues
  name                      = "${var.name_prefix}-${each.key}-dlq"
  message_retention_seconds = 1209600
  sqs_managed_sse_enabled   = true
}

resource "aws_sqs_queue" "this" {
  region                     = var.region
  for_each                   = local.queues
  name                       = "${var.name_prefix}-${each.key}"
  visibility_timeout_seconds = 900
  message_retention_seconds  = 345600
  receive_wait_time_seconds  = 10
  sqs_managed_sse_enabled    = true
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq[each.key].arn
    maxReceiveCount     = 5
  })
}

# --------------------------------------------------------------------------- worker

resource "aws_cloudwatch_log_group" "worker" {
  region            = var.region
  name              = "/ecs/${var.name_prefix}-worker"
  retention_in_days = var.log_retention_days
}

resource "aws_ecs_cluster" "this" {
  region = var.region
  name   = "${var.name_prefix}-worker"
  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

data "aws_iam_policy_document" "ecs_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "execution" {
  name               = "${var.name_prefix}-${var.region}-worker-execution" # IAM is global
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "execution_secrets" {
  count = length(var.secrets) > 0 ? 1 : 0
  name  = "read-secrets"
  role  = aws_iam_role.execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue"]
      Resource = values(var.secrets)
    }]
  })
}

resource "aws_iam_role" "task" {
  name               = "${var.name_prefix}-${var.region}-worker-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy" "task" {
  name   = "worker"
  role   = aws_iam_role.task.id
  policy = var.task_role_policy_json
}

resource "aws_iam_role_policy" "task_queues" {
  name = "queues"
  role = aws_iam_role.task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:ChangeMessageVisibility",
        "sqs:SendMessage", "sqs:GetQueueAttributes",
      ]
      Resource = [for q in aws_sqs_queue.this : q.arn]
    }]
  })
}

resource "aws_security_group" "worker" {
  region      = var.region
  name        = "${var.name_prefix}-worker"
  description = "Inventory worker tasks (egress only)"
  vpc_id      = var.vpc_id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_ecs_task_definition" "worker" {
  region                   = var.region
  family                   = "${var.name_prefix}-worker"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.cpu
  memory                   = var.memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([{
    name        = "worker"
    image       = var.image_uri
    essential   = true
    stopTimeout = 120
    environment = [for k, v in var.environment : { name = k, value = v }]
    secrets     = [for k, v in var.secrets : { name = k, valueFrom = v }]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.worker.name
        awslogs-region        = var.region
        awslogs-stream-prefix = "worker"
      }
    }
  }])
}

resource "aws_ecs_service" "worker" {
  region          = var.region
  name            = "${var.name_prefix}-worker"
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.worker.arn
  desired_count   = var.min_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = var.subnet_ids
    security_groups  = [aws_security_group.worker.id]
    assign_public_ip = false
  }

  lifecycle {
    ignore_changes = [desired_count] # owned by autoscaling
  }
}

# --------------------------------------------------------------------------- autoscaling

resource "aws_appautoscaling_target" "worker" {
  region             = var.region
  service_namespace  = "ecs"
  resource_id        = "service/${aws_ecs_cluster.this.name}/${aws_ecs_service.worker.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  min_capacity       = var.min_count
  max_capacity       = var.max_count
}

resource "aws_appautoscaling_policy" "backlog" {
  region             = var.region
  name               = "${var.name_prefix}-worker-backlog"
  service_namespace  = aws_appautoscaling_target.worker.service_namespace
  resource_id        = aws_appautoscaling_target.worker.resource_id
  scalable_dimension = aws_appautoscaling_target.worker.scalable_dimension
  policy_type        = "StepScaling"

  step_scaling_policy_configuration {
    adjustment_type         = "ExactCapacity"
    cooldown                = 120
    metric_aggregation_type = "Maximum"

    step_adjustment {
      metric_interval_upper_bound = 1
      scaling_adjustment          = var.min_count
    }
    step_adjustment {
      metric_interval_lower_bound = 1
      metric_interval_upper_bound = 100
      scaling_adjustment          = min(var.max_count, max(var.min_count, 2))
    }
    step_adjustment {
      metric_interval_lower_bound = 100
      metric_interval_upper_bound = 500
      scaling_adjustment          = min(var.max_count, max(var.min_count, 8))
    }
    step_adjustment {
      metric_interval_lower_bound = 500
      scaling_adjustment          = var.max_count
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "backlog" {
  region              = var.region
  alarm_name          = "${var.name_prefix}-worker-backlog"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  threshold           = 0
  alarm_actions       = [aws_appautoscaling_policy.backlog.arn]
  ok_actions          = [aws_appautoscaling_policy.backlog.arn]

  # waiting + in-flight: counting only visible messages would scale in (and stop) workers
  # that are still processing at the tail of a run
  metric_query {
    id          = "backlog"
    expression  = "scheduled_visible + ondemand_visible + scheduled_inflight + ondemand_inflight"
    label       = "waiting and in-flight messages"
    return_data = true
  }

  dynamic "metric_query" {
    for_each = { for pair in setproduct(local.queues, ["visible", "inflight"]) : "${pair[0]}_${pair[1]}" => pair }
    content {
      id = metric_query.key
      metric {
        namespace   = "AWS/SQS"
        metric_name = metric_query.value[1] == "visible" ? "ApproximateNumberOfMessagesVisible" : "ApproximateNumberOfMessagesNotVisible"
        period      = 60
        stat        = "Maximum"
        dimensions  = { QueueName = aws_sqs_queue.this[metric_query.value[0]].name }
      }
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "dlq" {
  region              = var.region
  for_each            = local.queues
  alarm_name          = "${var.name_prefix}-${each.key}-dlq-not-empty"
  alarm_description   = "Scan task messages failed repeatedly (BadMessage or crashes); inspect the DLQ."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = aws_sqs_queue.dlq[each.key].name }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = var.alarm_topic_arn == null ? [] : [var.alarm_topic_arn]
}

output "queue_urls" {
  value = { for k, q in aws_sqs_queue.this : k => q.url }
}
output "queue_arns" {
  value = { for k, q in aws_sqs_queue.this : k => q.arn }
}
output "dlq_names" {
  value = { for k, q in aws_sqs_queue.dlq : k => q.name }
}
output "log_group_name" {
  value = aws_cloudwatch_log_group.worker.name
}
output "security_group_id" {
  value = aws_security_group.worker.id
}
