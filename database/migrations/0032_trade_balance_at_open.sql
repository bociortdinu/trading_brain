-- 0032: account balance at the moment a LIVE position was opened.
--
-- The xStation CoreAPI does not publish a closed position's fill price or profit (that lives on
-- ipax.xtb.com, which returns nothing for our trades — see trading_hands/docs/
-- IPAX_CLOSED_POSITIONS.md). So when the broker closes a position on SL/TP we learn the outcome
-- from the ACCOUNT instead: balance moves only when something is REALIZED (unrealized P&L moves
-- equity, not balance), so the delta across a close is the realized result, exactly, with no
-- modelling.
--
-- Storing the baseline ON the trade rather than in memory is the point: the process can restart,
-- and a manager that had to remember the opening balance would lose the ability to price every
-- position that was open at the time.
--
-- Attribution is only unambiguous while at most one live position is in flight — otherwise the
-- delta covers several realizations and cannot be split. The manager enforces that and records
-- NULL rather than guessing.
ALTER TABLE trades ADD COLUMN balance_at_open NUMERIC;

COMMENT ON COLUMN trades.balance_at_open IS
    'Account balance (account currency) when this LIVE position was opened. NULL for shadow '
    'trades and for live rows written before this column existed. Used to derive realized P&L '
    'from the balance delta when the broker closes a position and does not report the fill.';
