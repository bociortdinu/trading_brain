-- 0016: ATOMIC reservation taken BEFORE the (paid) model call.
--
-- 0011 made the DECISION ROW unique per (input_fingerprint, run_id), which stops a duplicate
-- row — but not a duplicate CHARGE. The sequence was still:
--     SELECT fingerprint  ->  call the LLM  ->  INSERT ... ON CONFLICT DO NOTHING
-- so two concurrent workers both miss the SELECT, both pay, and only the loser's row is
-- discarded. The money is already spent by the time uniqueness is enforced.
--
-- A reservation is claimed BEFORE the call, atomically: exactly one worker can win the INSERT
-- ... ON CONFLICT, and only the winner is allowed to call the model.
--
-- CRASH RECOVERY: a worker that dies mid-call leaves 'in_progress' behind, which would block
-- that input forever. Each claim therefore carries a LEASE; once it expires the reservation can
-- be taken over. 'done' is terminal (never redo a completed decision); 'failed' is reclaimable
-- (a failed call may legitimately be retried).
--
-- This table is deliberately MUTABLE (unlike the append-only fact tables): a lease is state,
-- not a record of what happened.
CREATE TABLE decision_reservations (
    input_fingerprint TEXT        NOT NULL,
    run_id            TEXT        NOT NULL,
    status            TEXT        NOT NULL,
    worker            TEXT,
    reserved_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    lease_expires_at  TIMESTAMPTZ NOT NULL,
    -- CASCADE: a reservation only means something while its decision exists. If the decision is
    -- removed (retention, a wiped experiment), the claim must go too — otherwise a stale 'done'
    -- would tell a later run "already decided" for a decision that is no longer there.
    decision_id       BIGINT REFERENCES decisions (id) ON DELETE CASCADE,
    PRIMARY KEY (input_fingerprint, run_id),
    CONSTRAINT ck_reservation_status CHECK (status IN ('in_progress', 'done', 'failed'))
);

CREATE INDEX ix_reservations_run ON decision_reservations (run_id);
