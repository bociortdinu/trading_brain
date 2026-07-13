-- 0004: decision eligibility + durable provider/version conflict log.

-- An unexpected intraday gap (see data_quality) makes a snapshot ineligible for a decision.
ALTER TABLE market_snapshots ADD COLUMN eligible_for_decision BOOLEAN NOT NULL DEFAULT true;

-- Conflicts on (symbol, bar_close) from a different provider or pipeline version are
-- persisted (not left only in a log), so they can be audited rather than silently dropped.
CREATE TABLE snapshot_conflicts (
    id                        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ts                        TIMESTAMPTZ NOT NULL DEFAULT now(),
    symbol                    TEXT NOT NULL,
    bar_close                 TIMESTAMPTZ NOT NULL,
    existing_snapshot_id      BIGINT REFERENCES market_snapshots (id),
    existing_provider         TEXT,
    existing_pipeline_version TEXT,
    incoming_provider         TEXT,
    incoming_pipeline_version TEXT
);
CREATE INDEX ix_conflicts_symbol_bar ON snapshot_conflicts (symbol, bar_close);
