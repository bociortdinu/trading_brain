-- 0012: DATA CORRECTION — remove a FALSE claim from historical shadow-trade cost manifests.
--
-- Trades written before the manifest fix declare "commission" and "swap" as `modeled` even
-- though both rates were 0 (i.e. NOT applied): the persisted R-multiples are NOT net of real
-- financing, but the manifest claimed they were. We do not delete the rows (they stay auditable)
-- — we correct the claim: move commission/swap to `not_modeled` and record the real (zero) rates.
--
-- Targets only rows that actually carry the false claim: `modeled` contains commission/swap while
-- no non-zero rate is recorded. Idempotent (re-running matches nothing).
UPDATE trades SET costs = jsonb_build_object(
    'spread_pct',         costs->'spread_pct',
    'spread_provenance',  costs->'spread_provenance',
    'slippage_pct',       costs->'slippage_pct',
    'commission_pct',     0,
    'swap_pct_per_night', 0,
    'modeled',
        CASE WHEN COALESCE((costs->>'slippage_pct')::numeric, 0) <> 0
             THEN '["spread","gap_through_stop","latency","slippage"]'::jsonb
             ELSE '["spread","gap_through_stop","latency"]'::jsonb END,
    'not_modeled',
        CASE WHEN COALESCE((costs->>'slippage_pct')::numeric, 0) <> 0
             THEN '["commission","swap"]'::jsonb
             ELSE '["commission","swap","slippage"]'::jsonb END,
    'note', 'corrected by migration 0012: commission/swap rate 0 -> NOT applied, so this R is '
            'NOT net of real financing. Run also predates the single-position gate (overlapping '
            'entries = event-study, not an executable backtest).'
)
WHERE (costs->'modeled' @> '["commission"]'::jsonb OR costs->'modeled' @> '["swap"]'::jsonb)
  AND COALESCE((costs->>'commission_pct')::numeric, 0) = 0
  AND COALESCE((costs->>'swap_pct_per_night')::numeric, 0) = 0;
