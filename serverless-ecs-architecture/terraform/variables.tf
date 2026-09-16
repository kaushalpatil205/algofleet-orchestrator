variable "aws_region" {
  description = "AWS region for AlgoFleet ECS infrastructure (co-located with MT5 Bridge VPS in us-east-1)"
  type        = string
  default     = "us-east-1"
}

variable "trade_db_url" {
  description = "CockroachDB connection URL for trade history synchronization"
  type        = string
  default     = "postgresql://floyd:4NOc9B_RfdRuvNiCoU3A4w@mt5-strategy-engine-30775.j77.aws-ap-south-1.cockroachlabs.cloud:26257/defaultdb?sslmode=require"
  sensitive   = true
}

variable "enable_local_postgres" {
  description = "Whether to deploy the local PostgreSQL Fargate task and EFS filesystem. Set to false in production to utilize CockroachDB Serverless directly"
  type        = bool
  default     = false
}
