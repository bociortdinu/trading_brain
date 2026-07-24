-- 0014: CORRECTION — migration 0013 FABRICATED provenance for legacy spreads.
--
-- 0013 moved market_snapshots.spread_pct into spread_observations and hardcoded the provenance
-- to 'observed_xtb', assuming every stored spread had come from a live XTB quote. That was
-- WRONG: the backtest also wrote its MODELED spread (replay_spread_pct, 0.02) into the snapshot
-- column, so 43 of the 47 migrated rows now claim a broker quote was observed when none ever was
-- — the exact kind of false provenance this table exists to prevent.
--
-- The signal is unambiguous and was verified against the data before writing this:
--   basis IS NOT NULL  -> a real quote was taken (carries quote_time + basis.xtb_spread_pct that
--                         matches spread_pct). 4 rows, all genuine.
--   basis IS NULL      -> no quote was ever taken. 43 rows, all spread_pct = 0.0200, and every
--                         decision on those snapshots records spread_provenance = 'modeled'.
--
-- We relabel rather than delete: the values are real inputs that real decisions consumed; only
-- the provenance claim was false. 0013 stays as applied (history is not rewritten).
--
-- 'legacy_unknown' is deliberately NOT used: these are demonstrably the modeled backtest spread,
-- not an unknown. The check constraint from 0013 already allows 'modeled'.
UPDATE spread_observations
SET provenance = 'modeled',
    basis = jsonb_build_object(
        'note', 'relabelled by migration 0014: 0013 mislabelled this as observed_xtb. No quote '
                'was ever taken for this bar — it is the modeled replay spread the backtest used.'
    )
WHERE provenance = 'observed_xtb'
  AND basis IS NULL
  AND quote_time IS NULL;
