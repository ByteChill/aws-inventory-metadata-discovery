output "api_url" {
  description = "Internal API endpoint."
  value       = "http://${aws_lb.api.dns_name}"
}

output "state_machine_arn" {
  value = aws_sfn_state_machine.scan_run.arn
}

output "ecr_repository_url" {
  value = aws_ecr_repository.inventory.repository_url
}

output "database_proxy_endpoint" {
  value = aws_db_proxy.inventory.endpoint
}

output "adfs_secret_arn" {
  description = "Set its value manually: {\"username\": ..., \"password\": ...}"
  value       = aws_secretsmanager_secret.adfs.arn
}

output "cells" {
  description = "The INVENTORY_CELLS value given to every component."
  value       = local.cells
}

output "migrate_task_definition" {
  description = "Run once per release before the new image rolls out."
  value       = aws_ecs_task_definition.migrate.family
}

output "api_cluster" {
  value = aws_ecs_cluster.api.name
}

output "api_subnets_and_security_group" {
  description = "Network configuration for running the migration task."
  value       = { subnets = local.home.private_subnet_ids, security_group = aws_security_group.api.id }
}
