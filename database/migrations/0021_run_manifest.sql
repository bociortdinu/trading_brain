-- 0021: pin an experiment's execution config to its run_id.
--
-- Folding the execution config into the per-bar fingerprint stops a false crash-recovery, but it
-- does NOT stop two DIFFERENT configs being mixed into the same run_id: each config just mints new
-- fingerprints and both sets of decisions/trades land in the same experiment (measured: config A =
-- 51 decisions, config B = 102, all redeclared). A run_id must mean ONE frozen setup.
--
-- The first persisted use of a run_id records its execution-manifest hash here. A later run with
-- the same run_id but a different hash is REFUSED — pick a new run_id. Immutable: the app role may
-- INSERT and SELECT but not UPDATE (it is a fact about the experiment).
CREATE TABLE run_manifests (
    run_id        TEXT PRIMARY KEY,
    manifest_hash TEXT        NOT NULL,
    manifest      JSONB       NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
