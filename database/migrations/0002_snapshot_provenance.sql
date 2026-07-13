-- 0002: snapshot provenance (data lineage / reproducibility).
ALTER TABLE market_snapshots ADD COLUMN provider         TEXT;
ALTER TABLE market_snapshots ADD COLUMN provider_symbol  TEXT;
ALTER TABLE market_snapshots ADD COLUMN ingested_at      TIMESTAMPTZ;
ALTER TABLE market_snapshots ADD COLUMN intervals        JSONB;
ALTER TABLE market_snapshots ADD COLUMN pipeline_version TEXT;
