-- 0026: separate RETENTION role + documented append-only policy.
--
-- Two integrity properties, enforced by SEPARATION OF PRIVILEGE across roles:
--   * the APP role may INSERT facts but never UPDATE/DELETE them (enforced in database/migrate.py,
--     the one authoritative applier of the app role's grants — a REVOKE here would be undone by
--     that every-run block, so the app-role half lives there);
--   * a dedicated RETENTION role may DELETE aged rows but never INSERT or UPDATE — so pruning
--     cannot be used to forge or rewrite a fact, only to drop whole old rows.
-- Neither role can rewrite history: the app can't change a stored fact, retention can't fabricate
-- one. Admin/owner remains the only role that can do everything (migrations, ad-hoc fixes).

-- Retention role: NOLOGIN (assumed via `SET ROLE` by an owner/maintenance job, or granted to a
-- login maintenance user). Cluster-global, so guard creation for idempotency + the _test DB.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trading_brain_retention') THEN
        CREATE ROLE trading_brain_retention NOLOGIN;
    END IF;
END
$$;

GRANT USAGE ON SCHEMA public TO trading_brain_retention;

-- Prune-only on every data table: SELECT (decide what is aged) + DELETE (drop it). Deliberately
-- NO INSERT and NO UPDATE anywhere — retention can remove a whole old row but can neither create
-- nor alter one. schema_migrations is intentionally excluded (history is owner-only).
GRANT SELECT, DELETE ON
    market_snapshots, decisions, trades, spread_observations, snapshot_evaluations,
    llm_calls, run_manifests, snapshot_conflicts, decision_reservations,
    pipeline_runs, service_heartbeats, system_versions
    TO trading_brain_retention;

-- Document the policy on the tables themselves (visible in \d+ / information_schema).
COMMENT ON TABLE market_snapshots IS
    'IMMUTABLE observation. App role: INSERT + UPDATE(data_quality) only (one-way NULL->value '
    'back-fill); never DELETE. Prune via the trading_brain_retention role.';
COMMENT ON TABLE decisions IS
    'APPEND-ONLY fact (the LLM decision + its manifest). App role: INSERT only. Prune via '
    'trading_brain_retention.';
COMMENT ON TABLE spread_observations IS
    'APPEND-ONLY fact (a contextual spread at a time). App role: INSERT only.';
COMMENT ON TABLE snapshot_evaluations IS
    'APPEND-ONLY fact (an eligibility verdict). App role: INSERT only.';
COMMENT ON TABLE llm_calls IS
    'APPEND-ONLY fact (what an LLM call cost/returned). App role: INSERT only.';
COMMENT ON TABLE run_manifests IS
    'APPEND-ONLY fact (a run_id pinned to one config hash). App role: INSERT only.';
COMMENT ON TABLE snapshot_conflicts IS
    'APPEND-ONLY fact (a provider/version conflict log). App role: INSERT only.';
