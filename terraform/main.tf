# Control plane in the primary region: image registry, database, Redis, broker, planner,
# state machine, schedules and the API.

data "aws_caller_identity" "current" {}

locals {
  name_prefix = "inventory-${var.env}"
  account_id  = data.aws_caller_identity.current.account_id
  primary     = var.regions[var.primary_region]
  all_cidrs   = [for r in values(var.regions) : r.cidr_block]
  # fixed by AWS: IAM / Route 53 APIs, STS for the broker and ec2:DescribeRegions
  us_east_1    = "us-east-1"
  scan_regions = sort(keys(var.regions))

  queue_arns = flatten([
    for region in keys(var.regions) : [
      for kind in ["scheduled", "ondemand"] :
      "arn:aws:sqs:${region}:${local.account_id}:${local.name_prefix}-${kind}"
    ]
  ])

  # --- images: aws-inventory-discover/<component>:<version>, pushed to the primary region
  components      = toset(["broker", "planner", "api", "worker"])
  image_namespace = "aws-inventory-discover"
  image = {
    for c in local.components : c => {
      for region in keys(var.regions) :
      region => "${local.account_id}.dkr.ecr.${region}.amazonaws.com/${local.image_namespace}/${c}:${var.component_versions[c]}"
    }
  }

  # --- INVENTORY_ENDPOINTS: endpoint-specific DNS for cross-region calls only (private DNS is
  # not shared over VPC peering); same-region calls use the default DNS.
  dispatcher_endpoints = {
    for region, cfg in var.regions : region => merge(
      { sqs = cfg.endpoints["sqs"] },
      region == local.us_east_1 ? { ec2 = cfg.endpoints["ec2"] } : {},
    ) if region != var.primary_region
  }
  broker_endpoints = var.primary_region == local.us_east_1 ? {} : {
    (local.us_east_1) = { sts = var.regions[local.us_east_1].endpoints["sts"] }
  }
  worker_remote_endpoints = {
    (var.primary_region) = {
      lambda         = local.primary.endpoints["lambda"]
      secretsmanager = local.primary.endpoints["secretsmanager"]
    }
  }

  # --- environment per component (see inventory/config.py); Lambda allows 4 KB in total
  base_env = {
    INVENTORY_ENVIRONMENT    = var.env
    INVENTORY_LOG_LEVEL      = "INFO"
    INVENTORY_PRIMARY_REGION = var.primary_region
  }
  database_env = {
    INVENTORY_DATABASE_SECRET_ARN = aws_rds_cluster.inventory.master_user_secret[0].secret_arn
    INVENTORY_DATABASE_HOST       = aws_db_proxy.inventory.endpoint
  }
  dispatcher_env = merge(local.database_env, {
    INVENTORY_SCAN_REGIONS = jsonencode(local.scan_regions)
    INVENTORY_ENDPOINTS    = jsonencode(local.dispatcher_endpoints)
  })
  broker_env = merge(
    local.base_env,
    length(local.broker_endpoints) > 0 ? { INVENTORY_ENDPOINTS = jsonencode(local.broker_endpoints) } : {},
    {
      BROKER_ADFS_IDP_URL                = var.adfs_idp_url
      BROKER_ADFS_CREDENTIALS_SECRET_ARN = aws_secretsmanager_secret.adfs.arn
      BROKER_REDIS_URL                   = "rediss://${aws_elasticache_replication_group.broker.primary_endpoint_address}:6379/0"
      BROKER_REDIS_AUTH_SECRET_ARN       = aws_secretsmanager_secret.redis_auth.arn
      BROKER_KMS_KEY_ID                  = aws_kms_key.broker.arn
    },
  )
  planner_env = merge(local.base_env, local.dispatcher_env, {
    PLANNER_ACCOUNT_DIRECTORY_FUNCTION_NAME = var.account_directory_function_name
    PLANNER_DELETED_RETENTION_DAYS          = tostring(var.deleted_retention_days)
  })
  # ECS awslogs does not extract EMF; Lambda does
  api_env = merge(local.base_env, local.dispatcher_env, {
    INVENTORY_METRICS_MODE = "api"
    API_TRACING_ENABLED    = tostring(var.api_tracing_enabled)
  })
  worker_env = {
    for region in keys(var.regions) : region => merge(
      local.base_env,
      local.database_env,
      region == var.primary_region ? {} : { INVENTORY_ENDPOINTS = jsonencode(local.worker_remote_endpoints) },
      { INVENTORY_METRICS_MODE = "api", WORKER_REGION = region },
    )
  }
  migrate_env = merge(local.base_env, local.database_env)
}

# --------------------------------------------------------------------------- images

# The release workflow pushes images to the primary region; workers pull from their own
# region through replication.
resource "aws_ecr_repository" "component" {
  for_each             = local.components
  name                 = "${local.image_namespace}/${each.key}"
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_replication_configuration" "inventory" {
  count = var.manage_ecr_replication ? 1 : 0
  replication_configuration {
    rule {
      dynamic "destination" {
        for_each = setsubtract(keys(var.regions), [var.primary_region])
        content {
          region      = destination.value
          registry_id = local.account_id
        }
      }
      repository_filter {
        filter      = "${local.image_namespace}/worker"
        filter_type = "PREFIX_MATCH"
      }
    }
  }
}

# --------------------------------------------------------------------------- network

resource "aws_security_group" "lambda" {
  name        = "${local.name_prefix}-lambda"
  description = "Broker and planner Lambdas"
  vpc_id      = local.primary.vpc_id
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "database" {
  name        = "${local.name_prefix}-database"
  description = "Aurora and RDS Proxy: reachable from every region over peering"
  vpc_id      = local.primary.vpc_id
  ingress {
    from_port   = 5432
    to_port     = 5432
    protocol    = "tcp"
    cidr_blocks = local.all_cidrs
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "redis" {
  name        = "${local.name_prefix}-redis"
  description = "Broker cache"
  vpc_id      = local.primary.vpc_id
  ingress {
    from_port       = 6379
    to_port         = 6379
    protocol        = "tcp"
    security_groups = [aws_security_group.lambda.id]
  }
}

# --------------------------------------------------------------------------- database

resource "aws_db_subnet_group" "inventory" {
  name       = local.name_prefix
  subnet_ids = local.primary.private_subnet_ids
}

resource "aws_rds_cluster" "inventory" {
  cluster_identifier          = local.name_prefix
  engine                      = "aurora-postgresql"
  engine_version              = "16.6"
  database_name               = "inventory"
  master_username             = "inventory"
  manage_master_user_password = true
  db_subnet_group_name        = aws_db_subnet_group.inventory.name
  vpc_security_group_ids      = [aws_security_group.database.id]
  storage_encrypted           = true
  backup_retention_period     = 7
  deletion_protection         = var.env == "prod"
  skip_final_snapshot         = var.env != "prod"
  final_snapshot_identifier   = var.env == "prod" ? "${local.name_prefix}-final" : null

  lifecycle {
    ignore_changes = [engine_version] # AWS applies minor upgrades automatically
  }
}

resource "aws_rds_cluster_instance" "inventory" {
  count              = 2 # writer + reader
  identifier         = "${local.name_prefix}-${count.index}"
  cluster_identifier = aws_rds_cluster.inventory.id
  instance_class     = var.db_instance_class
  engine             = aws_rds_cluster.inventory.engine
  engine_version     = aws_rds_cluster.inventory.engine_version

  lifecycle {
    ignore_changes = [engine_version]
  }
}

data "aws_iam_policy_document" "rds_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["rds.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "db_proxy" {
  name               = "${local.name_prefix}-db-proxy"
  assume_role_policy = data.aws_iam_policy_document.rds_assume.json
}

resource "aws_iam_role_policy" "db_proxy" {
  name = "read-db-secret"
  role = aws_iam_role.db_proxy.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue"]
      Resource = aws_rds_cluster.inventory.master_user_secret[0].secret_arn
    }]
  })
}

resource "aws_db_proxy" "inventory" {
  name                   = local.name_prefix
  engine_family          = "POSTGRESQL"
  role_arn               = aws_iam_role.db_proxy.arn
  vpc_subnet_ids         = local.primary.private_subnet_ids
  vpc_security_group_ids = [aws_security_group.database.id]
  require_tls            = true

  auth {
    auth_scheme = "SECRETS"
    iam_auth    = "DISABLED"
    secret_arn  = aws_rds_cluster.inventory.master_user_secret[0].secret_arn
  }
}

resource "aws_db_proxy_default_target_group" "inventory" {
  db_proxy_name = aws_db_proxy.inventory.name
  connection_pool_config {
    max_connections_percent = 90
  }
}

resource "aws_db_proxy_target" "inventory" {
  db_proxy_name         = aws_db_proxy.inventory.name
  target_group_name     = aws_db_proxy_default_target_group.inventory.name
  db_cluster_identifier = aws_rds_cluster.inventory.cluster_identifier
}

# --------------------------------------------------------------------------- broker

resource "aws_kms_key" "broker" {
  description         = "Envelope encryption of credentials cached by the broker"
  enable_key_rotation = true
}

resource "aws_secretsmanager_secret" "adfs" {
  name        = "inventory/${var.env}/adfs"
  description = "ADFS service account: {\"username\": ..., \"password\": ...}. Value is set manually."
}

resource "random_password" "redis_auth" {
  length  = 48
  special = false
}

resource "aws_secretsmanager_secret" "redis_auth" {
  name = "inventory/${var.env}/redis-auth"
}

resource "aws_secretsmanager_secret_version" "redis_auth" {
  secret_id     = aws_secretsmanager_secret.redis_auth.id
  secret_string = random_password.redis_auth.result
}

resource "aws_elasticache_subnet_group" "broker" {
  name       = "${local.name_prefix}-broker"
  subnet_ids = local.primary.private_subnet_ids
}

resource "aws_elasticache_replication_group" "broker" {
  replication_group_id       = "${local.name_prefix}-broker"
  description                = "Broker SAML/credential cache"
  engine                     = "redis"
  engine_version             = "7.1"
  node_type                  = var.redis_node_type
  num_cache_clusters         = 2
  automatic_failover_enabled = true
  multi_az_enabled           = true
  subnet_group_name          = aws_elasticache_subnet_group.broker.name
  security_group_ids         = [aws_security_group.redis.id]
  at_rest_encryption_enabled = true
  transit_encryption_enabled = true
  auth_token                 = random_password.redis_auth.result
}

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "broker" {
  name               = "${local.name_prefix}-broker"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy_attachment" "broker_vpc" {
  role       = aws_iam_role.broker.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

resource "aws_iam_role_policy_attachment" "broker_xray" {
  role       = aws_iam_role.broker.name
  policy_arn = "arn:aws:iam::aws:policy/AWSXRayDaemonWriteAccess"
}

resource "aws_iam_role_policy" "broker" {
  name = "broker"
  role = aws_iam_role.broker.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = [aws_secretsmanager_secret.adfs.arn, aws_secretsmanager_secret.redis_auth.arn]
      },
      {
        Effect   = "Allow"
        Action   = ["kms:GenerateDataKey", "kms:Decrypt"]
        Resource = aws_kms_key.broker.arn
      },
    ]
  })
}

module "broker" {
  source               = "./modules/lambda_fn"
  name                 = "${local.name_prefix}-adfs-broker"
  image_uri            = local.image["broker"][var.primary_region]
  architecture         = var.lambda_architecture
  role_arn             = aws_iam_role.broker.arn
  timeout              = 60
  memory_size          = 512
  reserved_concurrency = 100
  subnet_ids           = local.primary.private_subnet_ids
  security_group_ids   = [aws_security_group.lambda.id]
  log_retention_days   = var.log_retention_days
  environment          = local.broker_env
}

# --------------------------------------------------------------------------- planner

resource "aws_iam_role" "planner" {
  name               = "${local.name_prefix}-planner"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy_attachment" "planner_vpc" {
  role       = aws_iam_role.planner.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

resource "aws_iam_role_policy_attachment" "planner_xray" {
  role       = aws_iam_role.planner.name
  policy_arn = "arn:aws:iam::aws:policy/AWSXRayDaemonWriteAccess"
}

data "aws_lambda_function" "account_directory" {
  function_name = var.account_directory_function_name
}

locals {
  # what every component that dispatches work or obtains credentials needs
  app_statements = [
    {
      Effect   = "Allow"
      Action   = ["lambda:InvokeFunction"]
      Resource = module.broker.arn
    },
    {
      Effect   = "Allow"
      Action   = ["sqs:SendMessage", "sqs:GetQueueUrl"]
      Resource = local.queue_arns
    },
    {
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue"]
      Resource = aws_rds_cluster.inventory.master_user_secret[0].secret_arn
    },
  ]
}

resource "aws_iam_role_policy" "planner" {
  name = "planner"
  role = aws_iam_role.planner.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(local.app_statements, [{
      Effect   = "Allow"
      Action   = ["lambda:InvokeFunction"]
      Resource = data.aws_lambda_function.account_directory.arn
    }])
  })
}

module "planner" {
  source             = "./modules/lambda_fn"
  name               = "${local.name_prefix}-planner"
  image_uri          = local.image["planner"][var.primary_region]
  architecture       = var.lambda_architecture
  role_arn           = aws_iam_role.planner.arn
  timeout            = 900
  memory_size        = 1024
  subnet_ids         = local.primary.private_subnet_ids
  security_group_ids = [aws_security_group.lambda.id]
  log_retention_days = var.log_retention_days
  environment        = local.planner_env
}

# --------------------------------------------------------------------------- state machine & schedules

data "aws_iam_policy_document" "states_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["states.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "scan_run" {
  name               = "${local.name_prefix}-scan-run"
  assume_role_policy = data.aws_iam_policy_document.states_assume.json
}

resource "aws_cloudwatch_log_group" "scan_run" {
  name              = "/aws/states/${local.name_prefix}-scan-run"
  retention_in_days = var.log_retention_days
}

resource "aws_iam_role_policy" "scan_run" {
  name = "scan-run"
  role = aws_iam_role.scan_run.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["lambda:InvokeFunction"]
        Resource = [module.planner.arn, "${module.planner.arn}:*"]
      },
      {
        # Distributed Map runs child executions of this state machine
        Effect   = "Allow"
        Action   = ["states:StartExecution", "states:DescribeExecution", "states:StopExecution"]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogDelivery", "logs:GetLogDelivery", "logs:UpdateLogDelivery",
          "logs:DeleteLogDelivery", "logs:ListLogDeliveries", "logs:PutResourcePolicy",
          "logs:DescribeResourcePolicies", "logs:DescribeLogGroups",
          "xray:PutTraceSegments", "xray:PutTelemetryRecords",
        ]
        Resource = "*"
      },
    ]
  })
}

resource "aws_sfn_state_machine" "scan_run" {
  name     = "${local.name_prefix}-scan-run"
  role_arn = aws_iam_role.scan_run.arn
  type     = "STANDARD"
  definition = templatefile("${path.module}/statemachines/scan_run.asl.json", {
    planner_function_arn = module.planner.arn
  })

  logging_configuration {
    log_destination        = "${aws_cloudwatch_log_group.scan_run.arn}:*"
    include_execution_data = false
    level                  = "ERROR"
  }

  tracing_configuration {
    enabled = true
  }
}

data "aws_iam_policy_document" "scheduler_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  name               = "${local.name_prefix}-scheduler"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
}

resource "aws_iam_role_policy" "scheduler" {
  name = "start"
  role = aws_iam_role.scheduler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["states:StartExecution"], Resource = aws_sfn_state_machine.scan_run.arn },
      { Effect = "Allow", Action = ["lambda:InvokeFunction"], Resource = module.planner.arn },
    ]
  })
}

resource "aws_scheduler_schedule" "scan_run" {
  name                = "${local.name_prefix}-scan-run"
  state               = var.schedules_enabled ? "ENABLED" : "DISABLED"
  schedule_expression = "rate(4 hours)"
  flexible_time_window {
    mode = "OFF"
  }
  target {
    arn      = aws_sfn_state_machine.scan_run.arn
    role_arn = aws_iam_role.scheduler.arn
    input    = jsonencode({})
  }
}

resource "aws_scheduler_schedule" "maintenance" {
  name                = "${local.name_prefix}-maintenance"
  state               = var.schedules_enabled ? "ENABLED" : "DISABLED"
  schedule_expression = "rate(15 minutes)"
  flexible_time_window {
    mode = "OFF"
  }
  target {
    arn      = module.planner.arn
    role_arn = aws_iam_role.scheduler.arn
    input    = jsonencode({ action = "maintenance" })
  }
}

# --------------------------------------------------------------------------- API

resource "aws_cloudwatch_log_group" "api" {
  name              = "/ecs/${local.name_prefix}-api"
  retention_in_days = var.log_retention_days
}

resource "aws_ecs_cluster" "api" {
  name = "${local.name_prefix}-api"
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

resource "aws_iam_role" "api_execution" {
  name               = "${local.name_prefix}-api-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy_attachment" "api_execution" {
  role       = aws_iam_role.api_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role" "api_task" {
  name               = "${local.name_prefix}-api-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy" "api_task" {
  name = "api"
  role = aws_iam_role.api_task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(local.app_statements, [
      {
        Effect   = "Allow"
        Action   = ["xray:PutTraceSegments", "xray:PutTelemetryRecords", "cloudwatch:PutMetricData"]
        Resource = "*"
      },
      {
        # the ADOT collector's default config also writes EMF logs
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"]
        Resource = "*"
      },
    ])
  })
}

resource "aws_security_group" "alb" {
  name        = "${local.name_prefix}-alb"
  description = "Internal API load balancer"
  vpc_id      = local.primary.vpc_id
  ingress {
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = local.all_cidrs
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "api" {
  name        = "${local.name_prefix}-api"
  description = "API tasks"
  vpc_id      = local.primary.vpc_id
  ingress {
    from_port       = 8080
    to_port         = 8080
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_lb" "api" {
  name               = "${local.name_prefix}-api"
  internal           = true
  load_balancer_type = "application"
  subnets            = local.primary.private_subnet_ids
  security_groups    = [aws_security_group.alb.id]
  idle_timeout       = 60
}

resource "aws_lb_target_group" "api" {
  name        = "${local.name_prefix}-api"
  port        = 8080
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = local.primary.vpc_id
  health_check {
    path    = "/readyz"
    matcher = "200"
  }
}

resource "aws_lb_listener" "api" {
  load_balancer_arn = aws_lb.api.arn
  port              = 80
  protocol          = "HTTP"
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
}

locals {
  api_container = {
    name         = "api"
    image        = local.image["api"][var.primary_region]
    essential    = true
    portMappings = [{ containerPort = 8080, protocol = "tcp" }]
    environment  = [for k, v in local.api_env : { name = k, value = v }]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.api.name
        awslogs-region        = var.primary_region
        awslogs-stream-prefix = "api"
      }
    }
  }
  adot_container = {
    name      = "adot"
    image     = var.adot_image
    essential = false
    command   = ["--config=/etc/ecs/ecs-default-config.yaml"]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.api.name
        awslogs-region        = var.primary_region
        awslogs-stream-prefix = "adot"
      }
    }
  }
}

resource "aws_ecs_task_definition" "api" {
  family                   = "${local.name_prefix}-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 1024
  memory                   = 2048
  execution_role_arn       = aws_iam_role.api_execution.arn
  task_role_arn            = aws_iam_role.api_task.arn
  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }
  # encode each container separately: the two containers have different shapes
  container_definitions = "[${join(",", concat(
    [jsonencode(local.api_container)],
    var.api_tracing_enabled ? [jsonencode(local.adot_container)] : [],
  ))}]"
}

resource "aws_ecs_service" "api" {
  name            = "${local.name_prefix}-api"
  cluster         = aws_ecs_cluster.api.id
  task_definition = aws_ecs_task_definition.api.arn
  desired_count   = var.api_desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = local.primary.private_subnet_ids
    security_groups  = [aws_security_group.api.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = 8080
  }

  depends_on = [aws_lb_listener.api]
}

# --------------------------------------------------------------------------- migrations

# One-off task: run before rolling out a release that contains migrations (deploy/README.md).
# It uses the worker image: for a new resource type the worker is released first.
resource "aws_ecs_task_definition" "migrate" {
  family                   = "${local.name_prefix}-migrate"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = aws_iam_role.api_execution.arn
  task_role_arn            = aws_iam_role.api_task.arn
  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }
  container_definitions = jsonencode([{
    name        = "migrate"
    image       = local.image["worker"][var.primary_region]
    essential   = true
    entryPoint  = ["python", "-m", "alembic"]
    command     = ["upgrade", "head"]
    environment = [for k, v in local.migrate_env : { name = k, value = v }]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.api.name
        awslogs-region        = var.primary_region
        awslogs-stream-prefix = "migrate"
      }
    }
  }])
}
