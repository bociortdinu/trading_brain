-- 0005: persist WHY a snapshot is ineligible (fail-closed at decision time, not by
-- dropping the observation). Ineligible snapshots are still stored for audit/replay.
ALTER TABLE market_snapshots ADD COLUMN ineligibility_reasons JSONB;
