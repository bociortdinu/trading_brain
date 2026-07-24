-- 0028: an explicit QUARANTINE state for a trade the current process cannot manage.
--
-- A trade opened under provider X must NOT be reconciled with provider Y's bars (R3-2 prevents
-- that contamination). But merely SKIPPING it left it status='open', and the position gate
-- (open_shadow_trades WHERE status='open') then blocked EVERY new decision for the symbol forever
-- (a deadlock). Quarantine is the honest resolution: the trade is moved out of 'open' into a
-- distinct, terminal-for-this-process state — it is NOT claimed to be reconciled, it stays fully
-- visible for a manual drain/migration, and it no longer blocks the gate.
ALTER TABLE trades ADD COLUMN quarantine_reason TEXT;
ALTER TABLE trades ADD COLUMN quarantined_at    TIMESTAMPTZ;

ALTER TABLE trades DROP CONSTRAINT ck_trade_status;
ALTER TABLE trades ADD  CONSTRAINT ck_trade_status
    CHECK (status IN ('open', 'closed', 'expired', 'quarantined'));

-- Consistency: a quarantined trade has no exit (it was never reconciled) but DOES carry a reason
-- and a timestamp, so the audit trail explains why it left 'open'.
ALTER TABLE trades DROP CONSTRAINT ck_trade_status_consistency;
ALTER TABLE trades ADD  CONSTRAINT ck_trade_status_consistency CHECK (
    (status = 'open'        AND closed_at IS NULL) OR
    (status = 'closed'      AND closed_at IS NOT NULL AND exit_price IS NOT NULL) OR
    (status = 'expired'     AND closed_at IS NOT NULL) OR
    (status = 'quarantined' AND closed_at IS NULL AND exit_price IS NULL
                            AND quarantine_reason IS NOT NULL AND quarantined_at IS NOT NULL)
);

-- Visibility: find quarantined trades needing a manual drain.
CREATE INDEX ix_trades_quarantined ON trades (symbol, quarantined_at DESC) WHERE status = 'quarantined';

COMMENT ON COLUMN trades.quarantine_reason IS
    'Why the trade was quarantined (e.g. frozen provider != current process provider). Set only '
    'when status=quarantined; the trade is NOT reconciled, only removed from the position gate.';
