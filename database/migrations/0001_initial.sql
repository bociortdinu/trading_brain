-- 0001_initial: core schema for trading_brain (PostgreSQL 17).
-- Applied by database/migrate.py with the application role. The database itself
-- is created separately by database/bootstrap.py with a privileged role.

-- 1. Market snapshot sent to analysis.
--    `bar_close` is the deterministic idempotency key (the M15 trigger bar's close
--    time); `ts` is the non-deterministic ingestion time. Re-running the same bar
--    must NOT create a duplicate -> UNIQUE(symbol, bar_close).
CREATE TABLE market_snapshots (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ts            TIMESTAMPTZ NOT NULL DEFAULT now(),
    bar_close     TIMESTAMPTZ NOT NULL,
    symbol        TEXT        NOT NULL,
    regime        TEXT        NOT NULL,
    adx_h1        NUMERIC(6, 2),
    atr_pct_m15   NUMERIC(8, 4),
    spread_pct    NUMERIC(8, 4),
    features      JSONB       NOT NULL,
    news_digest   JSONB,
    CONSTRAINT ck_snap_regime CHECK (regime IN ('bull_trend', 'bear_trend', 'range', 'choppy')),
    CONSTRAINT ck_snap_adx    CHECK (adx_h1 IS NULL OR (adx_h1 >= 0 AND adx_h1 <= 100)),
    CONSTRAINT ck_snap_atr    CHECK (atr_pct_m15 IS NULL OR atr_pct_m15 >= 0),
    CONSTRAINT ck_snap_spread CHECK (spread_pct IS NULL OR spread_pct >= 0),
    CONSTRAINT uq_snap_bar    UNIQUE (symbol, bar_close)
);
CREATE INDEX brin_snap_ts   ON market_snapshots USING BRIN (ts);
CREATE INDEX ix_snap_regime ON market_snapshots (symbol, regime, bar_close DESC);
CREATE INDEX gin_snap_feat  ON market_snapshots USING GIN (features);

-- 2. LLM decision (input + output + local risk governance + reproducibility manifest).
--    `direction` uses the INTERNAL vocabulary (BUY/SELL/NO_TRADE); the external
--    trading_hands contract (Buy/Sell/NoAction) is applied only in the purchase payload.
CREATE TABLE decisions (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    snapshot_id    BIGINT      NOT NULL REFERENCES market_snapshots (id),
    ts             TIMESTAMPTZ NOT NULL DEFAULT now(),
    model          TEXT        NOT NULL,
    direction      TEXT        NOT NULL,
    confidence     NUMERIC(4, 3) NOT NULL,       -- ORDINAL signal, NOT a probability
    sl_pct         NUMERIC(6, 3),                -- computed deterministically in Python
    tp_pct         NUMERIC(6, 3),                -- magnitude; sign applied at execution
    risk_verdict   TEXT        NOT NULL,
    risk_reason    TEXT,
    ai_input       JSONB       NOT NULL,
    ai_output      JSONB       NOT NULL,
    prompt_tokens  INT,
    output_tokens  INT,
    latency_ms     INT,
    cache_hit      BOOLEAN,
    mode           TEXT        NOT NULL DEFAULT 'shadow',
    -- reproducibility manifest
    prompt_version           TEXT,
    output_schema_version    TEXT,
    feature_pipeline_version TEXT,
    strategy_version         TEXT,
    risk_config_version      TEXT,
    data_provider            TEXT,
    input_hash               TEXT,
    as_of                    TIMESTAMPTZ,
    CONSTRAINT ck_dec_direction CHECK (direction IN ('BUY', 'SELL', 'NO_TRADE')),
    CONSTRAINT ck_dec_conf      CHECK (confidence >= 0 AND confidence <= 1),
    CONSTRAINT ck_dec_verdict   CHECK (risk_verdict IN ('approved', 'rejected')),
    CONSTRAINT ck_dec_mode      CHECK (mode IN ('shadow', 'live')),
    CONSTRAINT ck_dec_sl        CHECK (sl_pct IS NULL OR sl_pct > 0),
    CONSTRAINT ck_dec_tp        CHECK (tp_pct IS NULL OR tp_pct >= 0)
);
CREATE INDEX ix_dec_ts ON decisions (ts DESC);

-- 3. Trade + reconstructed outcome.
CREATE TABLE trades (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    decision_id    BIGINT      NOT NULL REFERENCES decisions (id),
    external_id    TEXT,                         -- from trading_hands (NULL in shadow)
    symbol         TEXT        NOT NULL,
    side           TEXT        NOT NULL,
    mode           TEXT        NOT NULL,
    entry_price    NUMERIC(12, 4) NOT NULL,
    sl_price       NUMERIC(12, 4) NOT NULL,
    tp_price       NUMERIC(12, 4) NOT NULL,
    opened_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    status         TEXT        NOT NULL DEFAULT 'open',
    exit_price     NUMERIC(12, 4),
    exit_reason    TEXT,
    closed_at      TIMESTAMPTZ,
    r_multiple     NUMERIC(8, 3),                -- (exit-entry)/(entry-sl), signed
    pnl            NUMERIC(14, 2),               -- live: authoritative; shadow: modeled
    r_pessimistic  NUMERIC(8, 3),                -- SL-first bound (both-hit intervals)
    r_optimistic   NUMERIC(8, 3),                -- TP-first bound
    ambiguous      BOOLEAN     NOT NULL DEFAULT false,
    CONSTRAINT ck_trade_side   CHECK (side IN ('buy', 'sell')),
    CONSTRAINT ck_trade_mode   CHECK (mode IN ('shadow', 'live')),
    CONSTRAINT ck_trade_status CHECK (status IN ('open', 'closed', 'expired')),
    CONSTRAINT ck_trade_prices CHECK (entry_price > 0 AND sl_price > 0 AND tp_price > 0),
    CONSTRAINT ck_trade_reason CHECK (
        exit_reason IS NULL OR exit_reason IN ('tp_hit', 'sl_hit', 'manual', 'timeout', 'ambiguous')
    ),
    CONSTRAINT ck_trade_status_consistency CHECK (
        (status = 'open'    AND closed_at IS NULL) OR
        (status = 'closed'  AND closed_at IS NOT NULL AND exit_price IS NOT NULL) OR
        (status = 'expired' AND closed_at IS NOT NULL)
    )
);
CREATE INDEX ix_trades_open   ON trades (status) WHERE status = 'open';
CREATE INDEX ix_trades_recent ON trades (symbol, closed_at DESC);
-- DEFERRED: external_id uniqueness. Do NOT make this UNIQUE until the ipax
-- cardinality (position / order / fill) is confirmed in Faza 4 — a single broker
-- position may map to multiple close records (partial fills). Non-unique for now.
CREATE INDEX ix_trades_external ON trades (external_id) WHERE external_id IS NOT NULL;

-- 4. System version manifest (reproducibility).
CREATE TABLE system_versions (
    id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ts        TIMESTAMPTZ NOT NULL DEFAULT now(),
    component TEXT NOT NULL,
    version   TEXT NOT NULL,
    details   JSONB
);
