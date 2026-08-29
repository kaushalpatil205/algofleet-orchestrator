aws secretsmanager put-secret-value \
    --secret-id algofleet/engine-config \
    --secret-string '{
        "BOT_TOKEN": "8549547329:AAGFPGoxM4CrNAjASBKwYGjbZwpLK3GD2lY",
        "CHAT_ID": "8113300560",
        "MT5_BRIDGE_URL": "https://exness-bridge-mt5.pickleballify.com/277746877/demo",
        "TRADE_DB_URL": "postgresql://user:pass@algofleet-db.xxxx.ap-south-1.rds.amazonaws.com:5432/algofleet",
        "MT5_API_KEY": "ak_qcWB24S08muPuRt0VJJTcvxMHrmuQNhfFfS8YQN66Y8"
    }'
