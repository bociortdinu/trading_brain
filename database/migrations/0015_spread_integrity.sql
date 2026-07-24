-- 0015: make the decision -> spread-observation link structurally trustworthy.
--
-- 0013's FK only said "spread_observation_id must be SOME observation". Nothing stopped a
-- decision on snapshot A from pointing at an observation belonging to snapshot B — which would
-- reintroduce, through the back door, exactly the mismatch 0013 set out to make impossible.
-- A COMPOSITE FK ties the pair together: the referenced observation must belong to the very
-- snapshot the decision was made on.
--
-- MATCH SIMPLE (the default) is what we want: when spread_observation_id IS NULL (a modeled /
-- replay constant, recorded in ai_input) the constraint is satisfied and the decision is free of
-- any observation — which is the honest representation of "no quote was taken".
ALTER TABLE spread_observations ADD CONSTRAINT uq_spread_obs_id_snapshot UNIQUE (id, snapshot_id);

ALTER TABLE decisions DROP CONSTRAINT decisions_spread_observation_id_fkey;

ALTER TABLE decisions ADD CONSTRAINT decisions_spread_obs_same_snapshot_fk
    FOREIGN KEY (spread_observation_id, snapshot_id)
    REFERENCES spread_observations (id, snapshot_id);
