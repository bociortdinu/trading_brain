-- 0022: backfill outcome_observed_at for the pre-0020 CLOSED shadow trades, then feedback can
-- require it (no COALESCE fallback to closed_at, which was a look-ahead risk: a result observed
-- in reality at 05:00 but with a NULL observed_at was offered to a 02:00 decision on closed_at).
--
-- JUSTIFIED backfill: every existing closed shadow trade was produced by a DETERMINISTIC BACKTEST,
-- where the outcome is known exactly at the bar it closed (there is no observation lag in replay).
-- So observed_at = closed_at is correct for these specific rows. New online trades set
-- outcome_observed_at explicitly (the reconcile wall-clock); feedback now EXCLUDES any closed row
-- that still lacks it rather than assuming it was known at closed_at.
UPDATE trades
   SET outcome_observed_at = closed_at
 WHERE mode = 'shadow'
   AND status <> 'open'
   AND outcome_observed_at IS NULL
   AND closed_at IS NOT NULL;
