-- 0008: make shadow trades idempotent, updatable and fully auditable for continuous runs.
--
-- Before: record_shadow_trade only INSERTed, so a decision could produce multiple trade
-- rows, an open trade could not be closed (only re-inserted), reconciliation was not
-- repeatable after a restart, and the spread/provenance/timeframe/cost policy was not
-- stored. After: a shadow trade is keyed by (decision_id, run_id) so re-running a shadow
-- pass UPSERTs the same row (open -> closed in place), experiments are separated by run_id,
-- and the modelling inputs are persisted.

ALTER TABLE trades ADD COLUMN run_id             TEXT NOT NULL DEFAULT 'unassigned';
ALTER TABLE trades ADD COLUMN timeframe          TEXT;      -- bar timeframe used to reconcile
ALTER TABLE trades ADD COLUMN timeout_bars       INT;       -- time-based exit policy
ALTER TABLE trades ADD COLUMN spread_pct         NUMERIC(8, 4);
ALTER TABLE trades ADD COLUMN spread_provenance  TEXT;      -- observed_xtb | modeled | historical
ALTER TABLE trades ADD COLUMN costs              JSONB;     -- what IS / is NOT modelled (honest)

-- Idempotency: one shadow trade per (decision, run). A re-run UPSERTs instead of duplicating.
CREATE UNIQUE INDEX uq_trade_decision_run ON trades (decision_id, run_id);
CREATE INDEX ix_trades_run ON trades (run_id);
