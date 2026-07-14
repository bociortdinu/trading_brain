-- 0010: persist the modeled slippage on a shadow trade so a still-open trade reconciled on a
-- LATER tick applies the same adverse exit slippage it was opened with (entry slippage is
-- already baked into entry_price/sl_price/tp_price; the exit leg needs the rate).
ALTER TABLE trades ADD COLUMN slippage_pct NUMERIC(8, 4);
