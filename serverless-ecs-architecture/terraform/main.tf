provider "aws" {
  region = "ap-south-1"
}

# 1. VPC Configuration (Public Subnets Only, No NAT Gateway for Sub-$40 Cost)
module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 5.0"

  name = "algofleet-ecs-vpc"
  cidr = "10.0.0.0/16"

  azs            = ["ap-south-1a", "ap-south-1b"]
  public_subnets = ["10.0.101.0/24", "10.0.102.0/24"]

  enable_nat_gateway = false

  public_subnet_tags = {
    "Name" = "algofleet-public-subnet"
  }
}

# 2. ECS Cluster (Fargate)
module "ecs" {
  source  = "terraform-aws-modules/ecs/aws"
  version = "~> 5.0"

  cluster_name = "algofleet-cluster"

  fargate_capacity_providers = {
    FARGATE = {
      default_capacity_provider_strategy = {
        weight = 0
      }
    }
    FARGATE_SPOT = {
      default_capacity_provider_strategy = {
        weight = 100
      }
    }
  }
}

# 3. IAM: ECS Task Execution Role
resource "aws_iam_role" "ecs_execution_role" {
  name = "algofleet-ecs-execution-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17",
    Statement = [{
      Action = "sts:AssumeRole",
      Effect = "Allow",
      Principal = {
        Service = "ecs-tasks.amazonaws.com"
      }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "ecs_execution_policy" {
  role       = aws_iam_role.ecs_execution_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "ecs_secrets_policy" {
  name = "ecs-secrets-policy"
  role = aws_iam_role.ecs_execution_role.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "secretsmanager:GetSecretValue"
        ]
        Resource = [aws_secretsmanager_secret.engine_config.arn]
      }
    ]
  })
}

# IAM: ECS Task Role (For the containers themselves)
resource "aws_iam_role" "ecs_task_role" {
  name = "algofleet-ecs-task-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17",
    Statement = [{
      Action = "sts:AssumeRole",
      Effect = "Allow",
      Principal = {
        Service = "ecs-tasks.amazonaws.com"
      }
    }]
  })
}

# 4. ECR Repositories
resource "aws_ecr_repository" "strategy_engine" {
  name                 = "strategy-engine"
  image_tag_mutability = "MUTABLE"
  force_delete         = true
}

resource "aws_ecr_repository" "trade_dashboard" {
  name                 = "trade-dashboard"
  image_tag_mutability = "MUTABLE"
  force_delete         = true
}

# 5. Secrets Manager
resource "aws_secretsmanager_secret" "engine_config" {
  name = "algofleet/engine-config"
}

# 6. EFS File System (For PostgreSQL Database Persistence)
resource "aws_efs_file_system" "postgres_data" {
  creation_token = "algofleet-postgres-data"
  encrypted      = true
  performance_mode = "generalPurpose"
  throughput_mode  = "bursting"
}

resource "aws_security_group" "efs_sg" {
  name        = "algofleet-efs-sg"
  vpc_id      = module.vpc.vpc_id
  ingress {
    from_port   = 2049
    to_port     = 2049
    protocol    = "tcp"
    cidr_blocks = [module.vpc.vpc_cidr_block]
  }
}

resource "aws_efs_mount_target" "postgres_mount" {
  count           = length(module.vpc.public_subnets)
  file_system_id  = aws_efs_file_system.postgres_data.id
  subnet_id       = module.vpc.public_subnets[count.index]
  security_groups = [aws_security_group.efs_sg.id]
}

# 7. GitHub Actions OIDC
data "aws_caller_identity" "current" {}

resource "aws_iam_openid_connect_provider" "github" {
  url = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = ["1c58a3a8518e8759bf075b76b750d4f2df264fcd"]
}

resource "aws_iam_role" "github_actions" {
  name = "algofleet-github-actions-v2"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action = "sts:AssumeRoleWithWebIdentity"
      Effect = "Allow"
      Principal = { Federated = aws_iam_openid_connect_provider.github.arn }
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
        }
        StringLike = {
          "token.actions.githubusercontent.com:sub" = "repo:kaushalpatil205/*"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "github_actions_ecs_ecr" {
  name = "ecs-ecr-deploy-policy"
  role = aws_iam_role.github_actions.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "ecr:GetAuthorizationToken",
          "ecr:BatchCheckLayerAvailability",
          "ecr:GetDownloadUrlForLayer",
          "ecr:BatchGetImage",
          "ecr:InitiateLayerUpload",
          "ecr:UploadLayerPart",
          "ecr:CompleteLayerUpload",
          "ecr:PutImage",
          "ecs:RegisterTaskDefinition",
          "ecs:UpdateService",
          "ecs:DescribeServices",
          "iam:PassRole"
        ]
        Resource = "*"
      }
    ]
  })
}
