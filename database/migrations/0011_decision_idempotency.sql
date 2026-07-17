-- 0011: end-to-end, ATOMIC idempotency for shadow decisions.
--
-- The trade-level UNIQUE(decision_id, run_id) cannot dedupe across re-runs because every re-run
-- mints a NEW decision_id. We instead give a decision a run-scoped identity:
--   input_fingerprint = hash(input_hash, model, prompt/strategy/risk versions, provider)
-- and make (input_fingerprint, run_id) UNIQUE within a run. insert_decision uses ON CONFLICT
-- DO NOTHING against this index, so two concurrent inserts cannot both create a row (no TOCTOU),
-- and a re-run can look up the existing decision and SKIP re-calling the (paid) LLM.
--
-- run_id is NULL for non-experiment decisions (app/decide, live), which the PARTIAL index
-- deliberately leaves unconstrained.
ALTER TABLE decisions ADD COLUMN run_id TEXT;
ALTER TABLE decisions ADD COLUMN input_fingerprint TEXT;

CREATE UNIQUE INDEX decisions_fingerprint_run_uq
    ON decisions (input_fingerprint, run_id)
    WHERE run_id IS NOT NULL AND input_fingerprint IS NOT NULL;
