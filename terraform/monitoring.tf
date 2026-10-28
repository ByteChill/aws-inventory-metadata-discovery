# Alarms and saved Logs Insights queries for the primary region.

locals {
  primary_alarm_actions = contains(keys(var.alarm_topic_arns), var.primary_region) ? [var.alarm_topic_arns[var.primary_region]] : []
  emf_alarms = {
    job-tasks-failed = {
      metric    = "JobTasksFailed"
      service   = "inventory-planner"
      threshold = 50
      text      = "Many scan tasks failed or were partial in the last scheduled run."
    }
    job-tasks-timed-out = {
      metric    = "JobTasksTimedOut"
      service   = "inventory-planner"
      threshold = 0
      text      = "Scan tasks timed out (workers too slow or stuck)."
    }
    schedule-overlap = {
      metric    = "ScheduleOverlap"
      service   = "inventory-planner"
      threshold = 0
      text      = "A scheduled run started while the previous one was still active."
    }
    adfs-login-failed = {
      metric    = "AdfsLoginFailed"
      service   = "inventory-broker"
      threshold = 0
      text      = "ADFS rejected the service account credentials (rotate/check the secret)."
    }
  }
  log_groups = [
    module.broker.log_group_name,
    module.planner.log_group_name,
    aws_cloudwatch_log_group.api.name,
    "/ecs/${local.name_prefix}-worker", # the primary-region worker; other regions have their own
  ]
}

resource "aws_cloudwatch_metric_alarm" "emf" {
  for_each            = local.emf_alarms
  alarm_name          = "${local.name_prefix}-${each.key}"
  alarm_description   = each.value.text
  namespace           = "Inventory"
  metric_name         = each.value.metric
  dimensions          = { Service = each.value.service }
  statistic           = "Sum"
  period              = 900
  evaluation_periods  = 1
  threshold           = each.value.threshold
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.primary_alarm_actions
}

resource "aws_cloudwatch_metric_alarm" "api_5xx" {
  alarm_name          = "${local.name_prefix}-api-5xx"
  alarm_description   = "The API returned server errors; trace them by X-Request-ID."
  namespace           = "AWS/ApplicationELB"
  metric_name         = "HTTPCode_Target_5XX_Count"
  dimensions          = { LoadBalancer = aws_lb.api.arn_suffix }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 5
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.primary_alarm_actions
}

resource "aws_cloudwatch_query_definition" "by_request_id" {
  name            = "inventory/${var.env}/trace-by-request-id"
  log_group_names = local.log_groups
  query_string    = <<-EOT
    fields @timestamp, service, level, message, job_id, task_id, account_id, region, resource_type, error_code, duration_ms
    | filter request_id = "REPLACE_WITH_REQUEST_ID" or correlation_id = "REPLACE_WITH_REQUEST_ID"
    | sort @timestamp asc
  EOT
}

resource "aws_cloudwatch_query_definition" "by_job" {
  name            = "inventory/${var.env}/job-summary"
  log_group_names = local.log_groups
  query_string    = <<-EOT
    filter job_id = "REPLACE_WITH_JOB_ID" and message = "task finished"
    | stats count(*) as tasks by task_status
  EOT
}

resource "aws_cloudwatch_query_definition" "throttling" {
  name            = "inventory/${var.env}/aws-throttling"
  log_group_names = local.log_groups
  query_string    = <<-EOT
    filter retry_attempts > 0 or error_code like /Throttl/
    | stats count(*) as calls by aws_service, aws_operation, account_id
    | sort calls desc
    | limit 50
  EOT
}
