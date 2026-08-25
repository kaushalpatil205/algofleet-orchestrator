-- Trade lifecycle persistence for the strategy engine.
-- One row per signal in `trades` (current state); append-only audit
-- trail in `trade_events`. Applied to the Neon Postgres instance.
--
-- Status lifecycle:
--   SIGNAL    signal generated, not yet sent to MT5
--   OPEN      MT5 accepted the order (retcode 10009), position live
--   REJECTED  MT5 rejected the order (bad retcode / no ticket)
--   CLOSED    position no longer open on MT5 (SL hit / manual close)
--   ERROR     unexpected failure while processing the signal

CREATE TABLE IF NOT EXISTS trades (
    id           BIGSERIAL PRIMARY KEY,
    strategy_id  TEXT NOT NULL,
    event_id     TEXT NOT NULL,
    symbol       TEXT NOT NULL,
    side         TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    magic        INTEGER,
    status       TEXT NOT NULL DEFAULT 'SIGNAL'
                 CHECK (status IN ('SIGNAL', 'OPEN', 'REJECTED', 'CLOSED', 'ERROR')),
    ticket       BIGINT,
    signal_price DOUBLE PRECISION,
    entry_price  DOUBLE PRECISION,
    -- Price the position actually closed at. MT5 P/L is money, not price
    -- distance (pnl = (exit-entry) x dir x qty x contract_size), so an exit
    -- price cannot be recovered from pnl without the per-symbol contract size
    -- — record the broker's own figure instead.
    exit_price   DOUBLE PRECISION,
    qty          DOUBLE PRECISION,
    hard_sl      DOUBLE PRECISION,
    current_sl   DOUBLE PRECISION,
    targets      JSONB NOT NULL DEFAULT '{}'::jsonb,
    trail_hits   JSONB NOT NULL DEFAULT '[]'::jsonb,
    retcode      INTEGER,
    error        TEXT,
    pnl          DOUBLE PRECISION,
    close_reason TEXT,
    telegram_sent BOOLEAN NOT NULL DEFAULT FALSE,
    tags         JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- source distinguishes live execution from imported backtest results;
    -- timeframe lets one strategy be compared/plotted across many TFs.
    source       TEXT NOT NULL DEFAULT 'live'
                 CHECK (source IN ('live', 'backtest')),
    timeframe    TEXT,
    backtest_id  BIGINT,
    opened_at    TIMESTAMPTZ,
    closed_at    TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (strategy_id, event_id)
);

CREATE INDEX IF NOT EXISTS idx_trades_strategy_status ON trades (strategy_id, status);
CREATE INDEX IF NOT EXISTS idx_trades_ticket ON trades (ticket) WHERE ticket IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_trades_created ON trades (created_at);
CREATE INDEX IF NOT EXISTS idx_trades_source ON trades (source);
CREATE INDEX IF NOT EXISTS idx_trades_backtest ON trades (backtest_id) WHERE backtest_id IS NOT NULL;

-- Added after the fact for databases created before source/timeframe existed.
ALTER TABLE trades ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'live';
ALTER TABLE trades ADD COLUMN IF NOT EXISTS timeframe TEXT;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS backtest_id BIGINT;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS exit_price DOUBLE PRECISION;

-- One row per uploaded backtest batch. Re-uploading a batch replaces its
-- trades (see visualizer backtest importer), so results stay updatable.
CREATE TABLE IF NOT EXISTS backtests (
    id           BIGSERIAL PRIMARY KEY,
    strategy_id  TEXT NOT NULL,
    label        TEXT,
    symbol       TEXT,
    note         TEXT,
    source_name  TEXT,          -- original filename
    trade_count  INTEGER NOT NULL DEFAULT 0,
    timeframes   JSONB NOT NULL DEFAULT '[]'::jsonb,
    net_pnl      DOUBLE PRECISION,
    uploaded_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_backtests_strategy ON backtests (strategy_id);

-- Uploaded OHLCV candles so backtest trades can be plotted even when the live
-- bridge has no history for that symbol/period. Keyed by symbol+timeframe+time.
CREATE TABLE IF NOT EXISTS candles (
    symbol     TEXT NOT NULL,
    timeframe  TEXT NOT NULL,
    ts         BIGINT NOT NULL,           -- ms epoch
    open       DOUBLE PRECISION,
    high       DOUBLE PRECISION,
    low        DOUBLE PRECISION,
    close      DOUBLE PRECISION,
    volume     DOUBLE PRECISION,
    PRIMARY KEY (symbol, timeframe, ts)
);

CREATE TABLE IF NOT EXISTS trade_events (
    id         BIGSERIAL PRIMARY KEY,
    trade_id   BIGINT NOT NULL REFERENCES trades(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    payload    JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_trade_events_trade ON trade_events (trade_id, created_at);
CREATE INDEX IF NOT EXISTS idx_trade_events_type ON trade_events (event_type);
