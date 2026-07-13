-- 0003: durable audit columns (not transient in-memory values).
--   data_quality:   per-timeframe gaps + verdict
--   basis_observed: feed vs XTB quote, WITH observation latency
-- Both are separate columns so a spread-less snapshot can be enriched later without
-- ever rewriting the computed `features`.
ALTER TABLE market_snapshots ADD COLUMN data_quality   JSONB;
ALTER TABLE market_snapshots ADD COLUMN basis_observed JSONB;
