-- 0029: a ledger of PROCESSED bars, so downtime is measured against the last bar the loop actually
-- handled — NOT the last bar it DECIDED on.
--
-- Bug (P0-C3): downtime was computed vs the last DECISION. But while a position is open, the
-- position gate intentionally skips deciding on each bar. Those bars were still PROCESSED (the loop
-- ran, observed them, respected the one-position policy), yet, measured against the last decision,
-- they were wrongly counted as a downtime gap. This ledger records every processed bar with its
-- outcome; `ok` marks a bar the loop handled cleanly (decided / position_open / ineligible / ...)
-- vs a failure (llm_failed / error). Downtime = open-market bars missing since the last `ok` bar.
CREATE TABLE processed_bars (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    symbol       TEXT        NOT NULL,
    provider     TEXT        NOT NULL,
    bar_close    TIMESTAMPTZ NOT NULL,
    run_id       TEXT        NOT NULL,
    outcome      TEXT        NOT NULL,   -- decided|rejected|position_open|held|already_decided|
                                         -- ineligible|prefiltered|llm_failed|error
    ok           BOOLEAN     NOT NULL,   -- true = handled cleanly (advances the downtime reference)
    processed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- One row per (symbol, provider, bar) across runs: the latest tick to process a bar wins, so a
    -- retry that succeeds can upgrade an earlier failure. Operational (upserted), not a fact.
    CONSTRAINT uq_processed_bar UNIQUE (symbol, provider, bar_close)
);
CREATE INDEX ix_pb_symbol_provider    ON processed_bars (symbol, provider, bar_close DESC);
CREATE INDEX ix_pb_symbol_provider_ok ON processed_bars (symbol, provider, bar_close DESC) WHERE ok;

COMMENT ON TABLE processed_bars IS
    'Operational ledger: every online bar the loop processed + its outcome. Downtime is measured '
    'against the last ok=true bar. App role: INSERT+UPDATE (upsert). Prune via retention.';

-- Retention may prune aged rows (app-role grants are applied per-table by database/migrate.py,
-- which adds processed_bars to the mutable/operational set).
GRANT SELECT, DELETE ON processed_bars TO trading_brain_retention;
