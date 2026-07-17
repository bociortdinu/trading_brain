-- 0017: three corrections to round 6.
--
-- 1. RESERVATION OWNERSHIP. complete_decision_reservation() updated by (fingerprint, run_id)
--    alone, so a STALE worker could close out a claim it no longer held. Reproduced:
--      A reserves -> A's lease expires -> B takes over -> A wakes up and completes
--      => status='done', worker='B', decision_id=NULL, and every later worker is told 'done'
--         for a decision that does not exist. The input is then permanently un-decidable.
--    A claim_token makes ownership explicit: it is minted per claim, and completion is a CAS
--    (WHERE status='in_progress' AND claim_token = :token) that must affect exactly one row.
--
-- 2. BLOCKED DISPOSITION. A bar can be approved yet not traded because a position was already
--    open. That disposition lived only in the in-memory report, so a resumed run could not tell
--    "approved and blocked" from "approved and traded" and reported something different from the
--    run it was continuing. Persist it.
--
-- 3. UNDO 0014's basis NOTE. 0014 fixed the fabricated provenance but wrote its explanation INTO
--    `basis` — which made all 47 rows basis IS NOT NULL and thereby destroyed the very
--    discriminator 0014 itself documented ("basis IS NOT NULL <=> a real quote was taken"). The
--    note belongs in a comment, not in a data column: a modeled spread has NO basis, so NULL it.
--    provenance='modeled' already carries the meaning.
ALTER TABLE decision_reservations ADD COLUMN claim_token TEXT;

ALTER TABLE decisions ADD COLUMN blocked_reason TEXT;

UPDATE spread_observations
SET basis = NULL
WHERE provenance = 'modeled'
  AND quote_time IS NULL
  AND basis ? 'note'
  AND NOT (basis ? 'xtb_spread_pct');
