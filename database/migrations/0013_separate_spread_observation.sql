-- 0013: separate the CONTEXTUAL spread from the IMMUTABLE OHLCV snapshot.
--
-- THE BUG THIS FIXES: market_snapshots carried spread_pct/basis_observed, which are NOT
-- properties of a closed bar — they come from a LIVE quote taken at some observation instant.
-- Because the row was ENRICHED with that quote later, one bar's snapshot could end up asserting
-- a spread that a decision on the same bar never used. Real example: snapshot 392 recorded an
-- observed spread of 0.0177 (quote at 11:08, for the 11:00 bar) while the replay decision FK'd
-- to it had decided on a MODELED 0.02. The chain therefore did not reproduce the frozen input.
--
-- THE MODEL NOW:
--   market_snapshots   = an immutable OHLCV+features observation of (symbol, bar_close). It says
--                        NOTHING about spread, so it can never contradict a decision.
--   spread_observations = append-only contextual facts ABOUT a snapshot: "at this instant, with
--                        this provenance, the spread was X". A bar can have zero (replay) or many.
--   decisions.spread_observation_id = exactly WHICH observation the decision consumed
--                        (NULL = a modeled/replay constant, which is recorded in ai_input).
CREATE TABLE spread_observations (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    snapshot_id BIGINT      NOT NULL REFERENCES market_snapshots (id) ON DELETE CASCADE,
    spread_pct  NUMERIC(8, 4) NOT NULL,
    provenance  TEXT        NOT NULL,
    quote_time  TIMESTAMPTZ,          -- broker tick time (may be NULL)
    observed_at TIMESTAMPTZ NOT NULL, -- when WE saw it (the latency-bearing clock)
    basis       JSONB,                -- feed-vs-broker basis, incl. lag/reliability
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_spread_obs_pct        CHECK (spread_pct >= 0),
    CONSTRAINT ck_spread_obs_provenance CHECK (provenance IN ('observed_xtb', 'modeled', 'historical')),
    -- append-only + idempotent: re-recording the SAME observation is a no-op, not a duplicate.
    CONSTRAINT uq_spread_obs            UNIQUE (snapshot_id, provenance, observed_at)
);

CREATE INDEX ix_spread_obs_snapshot ON spread_observations (snapshot_id);

-- Move the existing observations across (nothing is lost). Every stored spread came from an XTB
-- quote; fall back to the snapshot's insert time when the basis has no observed_at.
INSERT INTO spread_observations (snapshot_id, spread_pct, provenance, quote_time, observed_at, basis)
SELECT id,
       spread_pct,
       'observed_xtb',
       (basis_observed ->> 'quote_time')::timestamptz,
       COALESCE((basis_observed ->> 'observed_at')::timestamptz, ts),
       basis_observed
FROM market_snapshots
WHERE spread_pct IS NOT NULL;

-- The snapshot becomes a pure observation: no spread to enrich, hence nothing to mutate.
-- (ck_snap_spread is dropped with its column.)
ALTER TABLE market_snapshots DROP COLUMN spread_pct;
ALTER TABLE market_snapshots DROP COLUMN basis_observed;

ALTER TABLE decisions ADD COLUMN spread_observation_id BIGINT REFERENCES spread_observations (id);
