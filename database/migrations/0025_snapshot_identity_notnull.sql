-- 0025: make the snapshot SOURCE identity COMPLETE and NON-NULL.
--
-- 0024 widened uniqueness to (symbol, provider, pipeline_version, bar_close) so distinct sources
-- coexist, but left three gaps:
--   1. provider / provider_symbol / pipeline_version were still NULLABLE, so the unique key was
--      defeated by NULLs (Postgres treats NULL <> NULL): two source-less rows for the SAME bar
--      could both be inserted, reintroducing the duplicate/ambiguous observations the key exists
--      to prevent.
--   2. provider_symbol (the exact instrument the feed served — e.g. C:XAUUSD vs GOLD) was NOT part
--      of the identity, so re-pointing a (symbol, provider) at a different instrument silently
--      collided with the old series instead of being recorded as a distinct observation.
--   3. Source-scoped lookups (snapshot_spread_status) didn't filter by provider and could read a
--      DIFFERENT feed's row for the same bar. (Fixed in code: database/repository.py + app/jobs.py.)
--
-- BACKFILL FIRST. Rows written before provenance existed (pre-0002) or before provider was
-- mandatory may have NULLs. We do NOT know their true source, so we stamp HONEST sentinels rather
-- than inventing a real provider — the lineage stays truthful ("unknown"/"legacy"), and NOT NULL
-- can then be enforced without silently mislabelling history.
UPDATE market_snapshots SET provider         = 'unknown' WHERE provider IS NULL;
UPDATE market_snapshots SET pipeline_version = 'legacy'  WHERE pipeline_version IS NULL;
UPDATE market_snapshots SET provider_symbol  = symbol    WHERE provider_symbol IS NULL;

-- The identity now includes provider_symbol.
ALTER TABLE market_snapshots DROP CONSTRAINT uq_snap_source;
ALTER TABLE market_snapshots
    ADD CONSTRAINT uq_snap_source
    UNIQUE (symbol, provider, provider_symbol, pipeline_version, bar_close);

-- Enforce it: no part of the source identity may be NULL.
ALTER TABLE market_snapshots ALTER COLUMN provider         SET NOT NULL;
ALTER TABLE market_snapshots ALTER COLUMN provider_symbol  SET NOT NULL;
ALTER TABLE market_snapshots ALTER COLUMN pipeline_version SET NOT NULL;
