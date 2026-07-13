-- 0006: separate the CONTEXTUAL eligibility verdict from the IMMUTABLE market snapshot.
--
-- Before: market_snapshots carried a single eligible_for_decision boolean (+ reasons)
-- per (symbol, bar_close). But the SAME bar can be eligible in replay and stale online,
-- and a single boolean could hold only one verdict — the other was lost / overwritten,
-- with no record of the mode, the wall clock used, or the policy that produced it.
--
-- After: eligibility lives in its own table, keyed by (snapshot, mode, policy_version).
-- Online and replay are DISTINCT rows (neither overwrites the other); re-running the same
-- (mode, policy) refreshes that row in place; the policy that produced each verdict is
-- stored. The snapshot itself becomes a pure, immutable observation.

CREATE TABLE snapshot_evaluations (
    id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    snapshot_id       BIGINT NOT NULL REFERENCES market_snapshots (id) ON DELETE CASCADE,
    mode              TEXT NOT NULL,
    policy_version    TEXT NOT NULL,
    eligible          BOOLEAN NOT NULL,
    reasons           JSONB,
    policy            JSONB NOT NULL,
    evaluated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    ref_now           TIMESTAMPTZ,              -- the wall clock used (online only)
    feed_lag_seconds  NUMERIC(14, 1),
    quote_lag_seconds NUMERIC(14, 1),
    quote_present     BOOLEAN,
    CONSTRAINT ck_eval_mode CHECK (mode IN ('online', 'replay')),
    -- one CURRENT verdict per (snapshot, mode, policy): distinct modes/policies coexist.
    CONSTRAINT uq_eval_snapshot_mode_policy UNIQUE (snapshot_id, mode, policy_version)
);
CREATE INDEX ix_eval_snapshot ON snapshot_evaluations (snapshot_id);
CREATE INDEX ix_eval_eligible ON snapshot_evaluations (snapshot_id, eligible);

-- Backfill existing eligibility into the new model, then retire the columns. Historical
-- rows were produced by the online collector; the exact policy is unknown, so it is
-- tagged 'pre-0006-migrated'. Fresh evaluations supersede these per (mode, policy).
INSERT INTO snapshot_evaluations
    (snapshot_id, mode, policy_version, eligible, reasons, policy, evaluated_at)
SELECT id, 'online', 'pre-0006-migrated', eligible_for_decision, ineligibility_reasons,
       '{"logic": "pre-0006-migrated"}'::jsonb, ts
FROM market_snapshots;

ALTER TABLE market_snapshots DROP COLUMN eligible_for_decision;
ALTER TABLE market_snapshots DROP COLUMN ineligibility_reasons;
