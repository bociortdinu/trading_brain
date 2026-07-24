-- 0024: an observation's identity includes its data SOURCE.
--
-- UNIQUE(symbol, bar_close) treated two providers' observations of the SAME M15 bar as a
-- collision: the first source won, so switching feed (e.g. Polygon -> XTB) either conflicted or
-- skipped bars, and `latest_snapshot_bar_close(symbol)` mixed sources. But a bar observed via XTB
-- is a DIFFERENT observation from the same bar via Polygon (different venue/feed), and a new
-- pipeline version is a different derivation. Widen the key so distinct sources/pipeline versions
-- COEXIST as separate observations; same-source re-observation stays idempotent.
--
-- Backfill: none. The old key was STRICTER, so every existing row already satisfies the new
-- (looser) uniqueness — no duplicates can exist to reconcile.
ALTER TABLE market_snapshots DROP CONSTRAINT uq_snap_bar;
ALTER TABLE market_snapshots
    ADD CONSTRAINT uq_snap_source UNIQUE (symbol, provider, pipeline_version, bar_close);

-- Provider-scoped "latest bar" lookup (scheduler / downtime-gap detection).
CREATE INDEX IF NOT EXISTS ix_snap_symbol_provider
    ON market_snapshots (symbol, provider, bar_close DESC);
