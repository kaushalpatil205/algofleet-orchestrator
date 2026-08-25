aws secretsmanager create-secret \
    --name tradops/engine-config \
    --description "Shared secrets for the TradOps engine" \
    --secret-string '{
        "BOT_TOKEN": "8516308330:AAE5opWXPiGtpUdqtRD0u_LfyllXhUmK-7g",
        "CHAT_ID": "8113300560",
        "MT5_BRIDGE_URL": "https://exness-bridge-mt5.pickleballify.com/277746877/demo",
        "TRADE_DB_URL": "postgresql://user:pass@tradops-db.xxxx.ap-south-1.rds.amazonaws.com:5432/tradops",
        "MT5_API_KEY": "ak_YOUR_API_KEY_HERE"
    }' 
