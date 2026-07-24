-- 0023: explicit service liveness + per-tick execution audit for the operator dashboard.
--
-- Inferring "the scheduler is alive" from the newest market snapshot is wrong: the market may be
-- closed, the provider may have no new bar, or the process may simply be dead. service_heartbeats
-- answers process liveness; pipeline_runs answers what each tick attempted and how it ended.

CREATE TABLE service_heartbeats (
    service_name    TEXT PRIMARY KEY,
    instance_id     TEXT        NOT NULL,
    status          TEXT        NOT NULL,
    started_at      TIMESTAMPTZ NOT NULL,
    last_seen_at    TIMESTAMPTZ NOT NULL,
    last_success_at TIMESTAMPTZ,
    next_wake_at    TIMESTAMPTZ,
    last_error      TEXT,
    details         JSONB       NOT NULL DEFAULT '{}'::jsonb,
    git_commit      TEXT,
    CONSTRAINT ck_heartbeat_status CHECK (
        status IN ('starting', 'healthy', 'degraded', 'error', 'stopped')
    )
);

CREATE INDEX ix_heartbeats_seen ON service_heartbeats (last_seen_at DESC);

CREATE TABLE pipeline_runs (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    service_name    TEXT        NOT NULL,
    instance_id     TEXT        NOT NULL,
    run_kind        TEXT        NOT NULL,
    experiment_id   TEXT,
    symbol          TEXT,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ,
    status          TEXT        NOT NULL DEFAULT 'running',
    bars_processed  INT         NOT NULL DEFAULT 0,
    result          JSONB       NOT NULL DEFAULT '{}'::jsonb,
    error_type      TEXT,
    error_message   TEXT,
    git_commit      TEXT,
    CONSTRAINT ck_pipeline_run_status CHECK (
        status IN ('running', 'success', 'transient_error', 'failed', 'cancelled')
    ),
    CONSTRAINT ck_pipeline_run_terminal CHECK (
        (status = 'running' AND finished_at IS NULL) OR
        (status <> 'running' AND finished_at IS NOT NULL)
    )
);

CREATE INDEX ix_pipeline_runs_service ON pipeline_runs (service_name, started_at DESC);
CREATE INDEX ix_pipeline_runs_experiment ON pipeline_runs (experiment_id, started_at DESC)
    WHERE experiment_id IS NOT NULL;
CREATE INDEX ix_pipeline_runs_failed ON pipeline_runs (started_at DESC)
    WHERE status IN ('transient_error', 'failed');
