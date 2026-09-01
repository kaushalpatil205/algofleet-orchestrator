import json
import os

# Paths
VARIANTS_FILE = os.path.join(os.path.dirname(__file__), '../../variants/variants.json')
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), '../ecs_tasks')

os.makedirs(OUTPUT_DIR, exist_ok=True)

with open(VARIANTS_FILE, 'r') as f:
    variants = json.load(f)

for variant in variants:
    strategy_id = variant['id'].lower().replace('_', '-')
    
    # In a real environment, we'd inject the AWS Account ID and region via env vars
    ecr_image = "561789488706.dkr.ecr.ap-south-1.amazonaws.com/strategy-engine:latest"
    
    task_def = {
        "family": f"algofleet-strategy-{strategy_id}",
        "networkMode": "awsvpc",
        "requiresCompatibilities": ["FARGATE"],
        "cpu": "256",
        "memory": "512",
        "executionRoleArn": "arn:aws:iam::561789488706:role/algofleet-ecs-execution-role",
        "taskRoleArn": "arn:aws:iam::561789488706:role/algofleet-ecs-task-role",
        "containerDefinitions": [
            {
                "name": "strategy-bot",
                "image": ecr_image,
                "cpu": 256,
                "memory": 512,
                "essential": True,
                "environment": [
                    {"name": "STRATEGY_SCRIPT", "value": variant['script']},
                    {"name": "TRADE_DB_URL", "value": "postgresql://algofleet:supersecretpassword@postgres.algofleet.local:5432/algofleet"}
                ],
                "secrets": [
                    {
                        "name": "ENGINE_CONFIG_JSON",
                        "valueFrom": "arn:aws:secretsmanager:ap-south-1:561789488706:secret:algofleet/engine-config-xxxx"
                    }
                ],
                "logConfiguration": {
                    "logDriver": "awslogs",
                    "options": {
                        "awslogs-group": "/ecs/algofleet",
                        "awslogs-region": "ap-south-1",
                        "awslogs-stream-prefix": f"bot-{strategy_id}"
                    }
                }
            }
        ]
    }
    
    output_file = os.path.join(OUTPUT_DIR, f"{strategy_id}-task.json")
    with open(output_file, 'w') as out_f:
        json.dump(task_def, out_f, indent=2)

print(f"Successfully generated {len(variants)} ECS Task Definitions.")
