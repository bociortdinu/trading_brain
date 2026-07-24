-- 0031: FROZEN datasets — reproducible replay identity (Codex §5 P0-C1/P1-C4 dataset_id gap).
--
-- provider + provider_symbol + pipeline_version identify a SOURCE, but two different historical
-- exports from the same source (e.g. two CSV pulls) are logically identical under that key, so a
-- replay could not be pinned to the EXACT bytes it ran on. A `datasets` row is an immutable,
-- CONTENT-HASHED snapshot of a historical bar set (all timeframes) with provenance; a replay pins
-- to its dataset_id. This is REPLAY-only: live snapshots leave dataset_id NULL (the live stream is
-- not a frozen dataset), so the live path is unchanged.
CREATE TABLE datasets (
    dataset_id       TEXT        PRIMARY KEY,          -- sha256[:16] of the canonical series
    symbol           TEXT        NOT NULL,
    provider         TEXT        NOT NULL,
    provider_symbol  TEXT        NOT NULL,
    pipeline_version TEXT,                              -- pipeline in use when frozen (if known)
    timeframes       JSONB       NOT NULL,              -- timeframes included
    bar_counts       JSONB       NOT NULL,              -- {timeframe: count}
    first_bar        TIMESTAMPTZ,
    last_bar         TIMESTAMPTZ,
    sha256           TEXT        NOT NULL,              -- full digest (dataset_id is its prefix)
    source           TEXT,                              -- human description (csv path / backfill)
    provenance       JSONB,                             -- arbitrary provenance (key, args, ...)
    frozen_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_ds_counts CHECK (jsonb_typeof(bar_counts) = 'object')
);
CREATE INDEX ix_datasets_source ON datasets (symbol, provider, provider_symbol, frozen_at DESC);

COMMENT ON TABLE datasets IS
    'APPEND-ONLY: an immutable, content-hashed frozen historical bar set for reproducible replay. '
    'App role: INSERT only. Prune via retention.';

-- A snapshot MAY record the frozen dataset it came from (replay only; NULL for live). FK keeps it
-- honest — a dataset_id must reference a real frozen dataset. RESTRICT: a referenced dataset can't
-- be pruned out from under its snapshots.
ALTER TABLE market_snapshots ADD COLUMN dataset_id TEXT REFERENCES datasets (dataset_id);
CREATE INDEX ix_snap_dataset ON market_snapshots (dataset_id) WHERE dataset_id IS NOT NULL;

-- Append-only fact for the app role (baseline SELECT+INSERT from migrate.py); retention may prune.
GRANT SELECT, DELETE ON datasets TO trading_brain_retention;
