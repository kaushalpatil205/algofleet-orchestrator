import glob
import json
import os

# Paths
VARIANTS_FILE = os.path.join(os.path.dirname(__file__), '../../variants/variants.json')
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), '../ecs_tasks')

os.makedirs(OUTPUT_DIR, exist_ok=True)

# Configurable environment settings with production us-east-1 defaults
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
AWS_ACCOUNT_ID = os.environ.get("AWS_ACCOUNT_ID", "561789488706")

# Production CockroachDB URL shared across MT5 Bridge and Strategy Fleet
DEFAULT_TRADE_DB_URL = (
    "postgresql://floyd:4NOc9B_RfdRuvNiCoU3A4w@"
    "mt5-strategy-engine-30775.j77.aws-ap-south-1.cockroachlabs.cloud:26257/defaultdb?sslmode=require"
)
TRADE_DB_URL = os.environ.get("TRADE_DB_URL", DEFAULT_TRADE_DB_URL)

ENGINE_CONFIG_SECRET_ARN = os.environ.get(
    "ENGINE_CONFIG_SECRET_ARN",
    f"arn:aws:secretsmanager:{AWS_REGION}:{AWS_ACCOUNT_ID}:secret:algofleet/engine-config"
)

with open(VARIANTS_FILE, 'r') as f:
    variants_data = json.load(f)

# Handle both dictionary of lists (grouped by strategy) and flat list formats
variants_list = []
if isinstance(variants_data, dict):
    for group_key, items in variants_data.items():
        if isinstance(items, list):
            variants_list.extend(items)
        elif isinstance(items, dict):
            variants_list.append(items)
elif isinstance(variants_data, list):
    variants_list = variants_data
else:
    raise ValueError(f"Invalid variants.json format: expected dict or list, got {type(variants_data).__name__}")

# Clean up existing task definitions in OUTPUT_DIR before regenerating to prevent stale tasks
for old_file in glob.glob(os.path.join(OUTPUT_DIR, "*-task.json")):
    try:
        os.remove(old_file)
    except OSError:
        pass

ecr_image = f"{AWS_ACCOUNT_ID}.dkr.ecr.{AWS_REGION}.amazonaws.com/strategy-engine:latest"

for variant in variants_list:
    if not isinstance(variant, dict):
        continue

    raw_id = variant.get('id') or variant.get('file')
    if not raw_id:
        continue

    # Sanitize strategy ID and task family name (replace '.' and '_' with '-')
    strategy_id = raw_id.lower().replace('_', '-').replace('.', '-')
    family_name = f"algofleet-strategy-{strategy_id}".replace('_', '-').replace('.', '-')

    # Extract strategy script path from variants.json (strategy_script key)
    strategy_script = variant.get('strategy_script') or variant.get('script')
    if not strategy_script:
        raise ValueError(f"Variant '{raw_id}' missing 'strategy_script' or 'script' path")

    task_def = {
        "family": family_name,
        "networkMode": "awsvpc",
        "requiresCompatibilities": ["FARGATE"],
        "cpu": "256",
        "memory": "512",
        "executionRoleArn": f"arn:aws:iam::{AWS_ACCOUNT_ID}:role/algofleet-ecs-execution-role",
        "taskRoleArn": f"arn:aws:iam::{AWS_ACCOUNT_ID}:role/algofleet-ecs-task-role",
        "containerDefinitions": [
            {
                "name": "strategy-bot",
                "image": ecr_image,
                "cpu": 256,
                "memory": 512,
                "essential": True,
                "environment": [
                    {"name": "STRATEGY_SCRIPT", "value": strategy_script},
                    {"name": "TRADE_DB_URL", "value": TRADE_DB_URL}
                ],
                "secrets": [
                    {
                        "name": "ENGINE_CONFIG_JSON",
                        "valueFrom": ENGINE_CONFIG_SECRET_ARN
                    }
                ],
                "logConfiguration": {
                    "logDriver": "awslogs",
                    "options": {
                        "awslogs-group": "/ecs/algofleet",
                        "awslogs-region": AWS_REGION,
                        "awslogs-stream-prefix": f"bot-{strategy_id}"
                    }
                }
            }
        ]
    }

    output_file = os.path.join(OUTPUT_DIR, f"{strategy_id}-task.json")
    with open(output_file, 'w') as out_f:
        json.dump(task_def, out_f, indent=2)

print(f"Successfully generated {len(variants_list)} ECS Task Definitions in {OUTPUT_DIR}.")
