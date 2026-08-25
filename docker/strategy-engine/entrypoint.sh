#!/bin/bash
set -e

echo "Starting Strategy Engine Container..."

# Write the ENGINE_CONFIG_JSON secret to the file the engine reads
if [ -n "$ENGINE_CONFIG_JSON" ]; then
    echo "Injecting ENGINE_CONFIG_JSON from Secrets Manager..."
    echo "$ENGINE_CONFIG_JSON" > /app/Live/engine.json
else
    echo "WARNING: ENGINE_CONFIG_JSON is empty! Strategy might fail to connect to MT5/DB."
fi

# Background process: write a heartbeat file every 60s
# This satisfies the Kubernetes liveness probe independently of the python script
# The strategy's internal python heartbeat writes to the DB; this checks container health.
while true; do 
    touch /tmp/heartbeat
    sleep 60
done &

echo "Executing strategy script: $STRATEGY_SCRIPT"
exec python "$STRATEGY_SCRIPT"

