# One region cell per scanned region. Provider aliases cannot be chosen dynamically, so each
# supported region has its own module block that is enabled when present in var.regions.

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

module "cell_ap_east_1" {
  source    = "./modules/region_cell"
  count     = contains(keys(var.regions), "ap-east-1") ? 1 : 0
  providers = { aws = aws.ap_east_1 }

  name_prefix           = local.name_prefix
  region                = "ap-east-1"
  vpc_id                = var.regions["ap-east-1"].vpc_id
  subnet_ids            = var.regions["ap-east-1"].private_subnet_ids
  image_uri             = local.image["ap-east-1"]
  environment           = merge(local.ecs_env, { WORKER_REGION = "ap-east-1" })
  task_role_policy_json = local.worker_policy
  min_count             = var.worker_min_count
  max_count             = var.worker_max_count
  log_retention_days    = var.log_retention_days
  alarm_topic_arn       = lookup(var.alarm_topic_arns, "ap-east-1", null)
}

module "cell_eu_west_1" {
  source    = "./modules/region_cell"
  count     = contains(keys(var.regions), "eu-west-1") ? 1 : 0
  providers = { aws = aws.eu_west_1 }

  name_prefix           = local.name_prefix
  region                = "eu-west-1"
  vpc_id                = var.regions["eu-west-1"].vpc_id
  subnet_ids            = var.regions["eu-west-1"].private_subnet_ids
  image_uri             = local.image["eu-west-1"]
  environment           = merge(local.ecs_env, { WORKER_REGION = "eu-west-1" })
  task_role_policy_json = local.worker_policy
  min_count             = var.worker_min_count
  max_count             = var.worker_max_count
  log_retention_days    = var.log_retention_days
  alarm_topic_arn       = lookup(var.alarm_topic_arns, "eu-west-1", null)
}

module "cell_eu_west_2" {
  source    = "./modules/region_cell"
  count     = contains(keys(var.regions), "eu-west-2") ? 1 : 0
  providers = { aws = aws.eu_west_2 }

  name_prefix           = local.name_prefix
  region                = "eu-west-2"
  vpc_id                = var.regions["eu-west-2"].vpc_id
  subnet_ids            = var.regions["eu-west-2"].private_subnet_ids
  image_uri             = local.image["eu-west-2"]
  environment           = merge(local.ecs_env, { WORKER_REGION = "eu-west-2" })
  task_role_policy_json = local.worker_policy
  min_count             = var.worker_min_count
  max_count             = var.worker_max_count
  log_retention_days    = var.log_retention_days
  alarm_topic_arn       = lookup(var.alarm_topic_arns, "eu-west-2", null)
}

module "cell_us_east_1" {
  source    = "./modules/region_cell"
  count     = contains(keys(var.regions), "us-east-1") ? 1 : 0
  providers = { aws = aws.us_east_1 }

  name_prefix           = local.name_prefix
  region                = "us-east-1"
  vpc_id                = var.regions["us-east-1"].vpc_id
  subnet_ids            = var.regions["us-east-1"].private_subnet_ids
  image_uri             = local.image["us-east-1"]
  environment           = merge(local.ecs_env, { WORKER_REGION = "us-east-1" })
  task_role_policy_json = local.worker_policy
  min_count             = var.worker_min_count
  max_count             = var.worker_max_count
  log_retention_days    = var.log_retention_days
  alarm_topic_arn       = lookup(var.alarm_topic_arns, "us-east-1", null)
}
