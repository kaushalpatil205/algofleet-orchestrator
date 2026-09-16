output "cluster_name" {
  description = "The name of the ECS cluster"
  value       = module.ecs.cluster_name
}

output "cluster_id" {
  description = "The ID of the ECS cluster"
  value       = module.ecs.cluster_id
}

output "cluster_arn" {
  description = "The ARN of the ECS cluster"
  value       = module.ecs.cluster_arn
}

output "strategy_engine_ecr_url" {
  description = "The URL of the Strategy Engine ECR repository"
  value       = aws_ecr_repository.strategy_engine.repository_url
}

output "trade_dashboard_ecr_url" {
  description = "The URL of the Trade Dashboard ECR repository"
  value       = aws_ecr_repository.trade_dashboard.repository_url
}

output "secrets_manager_arn" {
  description = "The ARN of the Secrets Manager vault"
  value       = aws_secretsmanager_secret.engine_config.arn
}

output "github_actions_role_arn" {
  description = "IAM Role ARN for GitHub Actions OIDC — add this to GitHub repo secrets as AWS_ROLE_ARN"
  value       = aws_iam_role.github_actions.arn
}
