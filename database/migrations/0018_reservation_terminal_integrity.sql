-- 0018: make a reservation's TERMINAL state trustworthy at the DB level.
--
-- 0016/0017 gave the reservation a status, a lease and an owner token, but nothing tied those
-- together, so the DB happily stored a 'done' reservation with decision_id = NULL. Reproduced:
--   complete(status='done', decision_id=NULL) -> ('done', NULL) accepted; every later worker is
--   then told "already decided" for a decision that does not exist. 'done' MUST mean a real,
--   complete chain, not merely "a decisions row exists".
--
-- Three DB guarantees (the repository enforces the same, but the DB is the backstop):
--   1. done          => decision_id IS NOT NULL  (a completed decision points at a real row)
--   2. in_progress   => claim_token IS NOT NULL   (an owned claim always has an owner token)
--   3. decision_id, when set, belongs to the SAME (input_fingerprint, run_id) as the
--      reservation — a reservation can't be closed out against another input's decision.
--
-- REPAIR BEFORE CONSTRAINING. A DB that already ran the old code may hold rows that violate
-- these — a 'done' with no decision, an 'in_progress' with no token, or a decision_id whose
-- (fingerprint, run_id) doesn't match. ADD CONSTRAINT would then FAIL. Reset such rows to
-- 'failed' first: a failed reservation is reclaimable, so a later run re-does that input cleanly
-- and no corrupt state survives. (Fresh databases have an empty table here — this is a no-op.)
UPDATE decision_reservations SET status = 'failed', decision_id = NULL
 WHERE (status = 'done' AND decision_id IS NULL)
    OR (status = 'in_progress' AND claim_token IS NULL)
    OR (decision_id IS NOT NULL AND NOT EXISTS (
          SELECT 1 FROM decisions d
          WHERE d.id = decision_reservations.decision_id
            AND d.input_fingerprint = decision_reservations.input_fingerprint
            AND d.run_id = decision_reservations.run_id));

ALTER TABLE decision_reservations
    ADD CONSTRAINT ck_reservation_done_has_decision
    CHECK (status <> 'done' OR decision_id IS NOT NULL);

ALTER TABLE decision_reservations
    ADD CONSTRAINT ck_reservation_inprogress_has_token
    CHECK (status <> 'in_progress' OR claim_token IS NOT NULL);

-- Composite FK needs a matching UNIQUE to reference. id is already unique, so (id,
-- input_fingerprint, run_id) is trivially unique — this just makes it referenceable.
ALTER TABLE decisions
    ADD CONSTRAINT uq_decision_id_fp_run UNIQUE (id, input_fingerprint, run_id);

-- Replace the id-only FK with the composite one (keep ON DELETE CASCADE: a reservation is
-- meaningless without its decision). MATCH SIMPLE means a NULL decision_id skips the check, so
-- 'in_progress'/'failed' rows (no decision yet) are unaffected.
ALTER TABLE decision_reservations DROP CONSTRAINT decision_reservations_decision_id_fkey;

ALTER TABLE decision_reservations
    ADD CONSTRAINT decision_reservation_decision_same_fp_run_fk
    FOREIGN KEY (decision_id, input_fingerprint, run_id)
    REFERENCES decisions (id, input_fingerprint, run_id) ON DELETE CASCADE;
