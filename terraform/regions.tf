# One region cell per scan region. Every resource in the module sets its own `region`
# (AWS provider 6.x), so a new region only needs an entry in var.regions.

locals {
  worker_policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(local.app_statements, [{
      Effect   = "Allow"
      Action   = ["cloudwatch:PutMetricData"]
      Resource = "*"
    }])
  })
}

module "cell" {
  source   = "./modules/region_cell"
  for_each = var.regions

  name_prefix           = local.name_prefix
  region                = each.key
  vpc_id                = each.value.vpc_id
  subnet_ids            = each.value.private_subnet_ids
  image_uri             = local.image["worker"][each.key]
  environment           = local.worker_env[each.key]
  task_role_policy_json = local.worker_policy
  min_count             = var.worker_min_count
  max_count             = var.worker_max_count
  log_retention_days    = var.log_retention_days
  alarm_topic_arn       = lookup(var.alarm_topic_arns, each.key, null)
}
