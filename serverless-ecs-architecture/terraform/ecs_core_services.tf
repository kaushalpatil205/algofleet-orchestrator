# Security Group for ECS Tasks
resource "aws_security_group" "ecs_tasks_sg" {
  name        = "algofleet-ecs-tasks-sg"
  vpc_id      = module.vpc.vpc_id

  # Allow postgres traffic from within the VPC
  ingress {
    from_port   = 5432
    to_port     = 5432
    protocol    = "tcp"
    cidr_blocks = [module.vpc.vpc_cidr_block]
  }

  # Allow all outbound traffic
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# CloudWatch Log Group
resource "aws_cloudwatch_log_group" "ecs_logs" {
  name              = "/ecs/algofleet"
  retention_in_days = 7
}

# PostgreSQL Task Definition
resource "aws_ecs_task_definition" "postgres" {
  family                   = "algofleet-postgres"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = "256"
  memory                   = "512"
  execution_role_arn       = aws_iam_role.ecs_execution_role.arn
  task_role_arn            = aws_iam_role.ecs_task_role.arn

  volume {
    name = "postgres-storage"
    efs_volume_configuration {
      file_system_id          = aws_efs_file_system.postgres_data.id
      transit_encryption      = "ENABLED"
    }
  }

  container_definitions = jsonencode([
    {
      name      = "postgres"
      image     = "postgres:15-alpine"
      cpu       = 256
      memory    = 512
      essential = true
      environment = [
        { name = "POSTGRES_USER", value = "algofleet" },
        { name = "POSTGRES_PASSWORD", value = "supersecretpassword" },
        { name = "POSTGRES_DB", value = "algofleet" },
        { name = "PGDATA", value = "/var/lib/postgresql/data/pgdata" }
      ]
      mountPoints = [
        {
          sourceVolume  = "postgres-storage"
          containerPath = "/var/lib/postgresql/data"
        }
      ]
      portMappings = [
        {
          containerPort = 5432
          hostPort      = 5432
        }
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.ecs_logs.name
          "awslogs-region"        = "ap-south-1"
          "awslogs-stream-prefix" = "postgres"
        }
      }
    }
  ])
}

# PostgreSQL ECS Service
resource "aws_ecs_service" "postgres" {
  name            = "algofleet-postgres"
  cluster         = module.ecs.cluster_id
  task_definition = aws_ecs_task_definition.postgres.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = module.vpc.public_subnets
    security_groups  = [aws_security_group.ecs_tasks_sg.id]
    assign_public_ip = true
  }

  # For internal DNS service discovery
  # (In a real setup we would add AWS Cloud Map namespace here so bots can resolve 'postgres.algofleet.local')
}

# Dashboard Task Definition (with Cloudflare Tunnel Sidecar)
resource "aws_ecs_task_definition" "dashboard" {
  family                   = "algofleet-dashboard"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = "512"
  memory                   = "1024"
  execution_role_arn       = aws_iam_role.ecs_execution_role.arn
  task_role_arn            = aws_iam_role.ecs_task_role.arn

  container_definitions = jsonencode([
    {
      name      = "dashboard"
      image     = "${aws_ecr_repository.trade_dashboard.repository_url}:latest"
      cpu       = 256
      memory    = 512
      essential = true
      portMappings = [
        {
          containerPort = 8000
          hostPort      = 8000
        }
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.ecs_logs.name
          "awslogs-region"        = "ap-south-1"
          "awslogs-stream-prefix" = "dashboard"
        }
      }
    },
    {
      name      = "cloudflared"
      image     = "cloudflare/cloudflared:latest"
      cpu       = 256
      memory    = 512
      essential = true
      command   = ["tunnel", "--no-autoupdate", "run", "--token", "YOUR_CLOUDFLARE_TUNNEL_TOKEN"]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.ecs_logs.name
          "awslogs-region"        = "ap-south-1"
          "awslogs-stream-prefix" = "cloudflared"
        }
      }
    }
  ])
}

# Dashboard ECS Service
resource "aws_ecs_service" "dashboard" {
  name            = "algofleet-dashboard"
  cluster         = module.ecs.cluster_id
  task_definition = aws_ecs_task_definition.dashboard.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = module.vpc.public_subnets
    security_groups  = [aws_security_group.ecs_tasks_sg.id]
    assign_public_ip = true
  }
}
