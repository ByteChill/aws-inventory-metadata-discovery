output "api_url" {
  description = "Internal API endpoint."
  value       = "http://${aws_lb.api.dns_name}"
}

output "state_machine_arn" {
  value = aws_sfn_state_machine.scan_run.arn
}

output "ecr_repository_urls" {
  description = "Push each component's image here (primary region), tagged with its version."
  value       = { for c, repo in aws_ecr_repository.component : c => repo.repository_url }
}

output "database_proxy_endpoint" {
  value = aws_db_proxy.inventory.endpoint
}

output "adfs_secret_arn" {
  description = "Set its value manually: {\"username\": ..., \"password\": ...}"
  value       = aws_secretsmanager_secret.adfs.arn
}

output "worker_environments" {
  description = "The environment of each region's worker (for checking cross-region endpoints)."
  value       = local.worker_env
}

output "migrate_task_definition" {
  description = "Run with the new worker image before releasing a version that contains migrations."
  value       = aws_ecs_task_definition.migrate.family
}

output "api_cluster" {
  value = aws_ecs_cluster.api.name
}

output "api_subnets_and_security_group" {
  description = "Network configuration for running the migration task."
  value       = { subnets = local.primary.private_subnet_ids, security_group = aws_security_group.api.id }
}
