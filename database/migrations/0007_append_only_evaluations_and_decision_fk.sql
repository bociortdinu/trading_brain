-- 0007: make eligibility evaluations APPEND-ONLY and let a decision reference the EXACT,
-- immutable verdict that authorized it.
--
-- Before: snapshot_evaluations had UNIQUE(snapshot_id, mode, policy_version) with
-- upsert-in-place, so a later re-evaluation of the same (mode, policy) OVERWROTE the prior
-- verdict — a decision could not point at the frozen verdict it was made under. Now every
-- evaluation is a new immutable row; the "current" verdict is the latest by evaluated_at,
-- and decisions carry an FK to the specific evaluation id.

-- Append-only: drop the uniqueness; index the latest-lookup instead.
ALTER TABLE snapshot_evaluations DROP CONSTRAINT uq_eval_snapshot_mode_policy;
CREATE INDEX ix_eval_latest
    ON snapshot_evaluations (snapshot_id, mode, policy_version, evaluated_at DESC);

-- Correct the 0006 backfill mode assumption. Only rows with ONLINE-ONLY evidence in their
-- reasons (stale_feed / missing_xtb_quote / stale_quote — never emitted in replay) are
-- DEMONSTRABLY online. Any pre-0006 row without such evidence had its mode ASSUMED; delete
-- it rather than assert a mode we cannot demonstrate (the snapshot simply has no historical
-- evaluation). Demonstrable rows are relabelled to make the inference explicit.
DELETE FROM snapshot_evaluations
 WHERE policy_version = 'pre-0006-migrated'
   AND NOT (reasons::text LIKE '%stale_feed%'
            OR reasons::text LIKE '%missing_xtb_quote%'
            OR reasons::text LIKE '%stale_quote%');
UPDATE snapshot_evaluations
   SET policy_version = 'pre-0006-inferred-online'
 WHERE policy_version = 'pre-0006-migrated';

-- A decision references the immutable evaluation that authorized it.
ALTER TABLE decisions
    ADD COLUMN evaluation_id BIGINT REFERENCES snapshot_evaluations (id);
CREATE INDEX ix_decisions_evaluation ON decisions (evaluation_id);
