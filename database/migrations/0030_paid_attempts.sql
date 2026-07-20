-- 0030: per-HTTP-attempt ledger for PAID AI calls — the audit + budget backbone of the central
-- financial gateway.
--
-- Problem (Codex §4.3): the runner counted LOGICAL decisions and audited only the successful result
-- atomically with the decision, so a timeout AFTER the request was accepted could leave real cost
-- with no local trace, and there was no USD budget at all. This table records EACH attempt: a row
-- is written 'started' BEFORE the HTTP request (with a worst-case est_cost_usd used for the atomic
-- budget reservation), then finalized to 'completed'/'timeout'/'error' with the real usage. A row
-- stuck at 'started' (process died mid-request) is a visible signal to reconcile, never "zero cost".
-- Budget accounting is DERIVED from this table (sum over run / UTC day / UTC month).
CREATE TABLE paid_attempts (
    id                 BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id             TEXT        NOT NULL,
    context            TEXT        NOT NULL,   -- 'shadow.runner' | 'app.decide' | 'app.llm_smoke'
    model              TEXT        NOT NULL,
    input_hash         TEXT        NOT NULL,
    attempt_no         INT         NOT NULL,   -- retry index for the same logical decision (0-based)
    status             TEXT        NOT NULL,   -- started|completed|timeout|error|unknown
    request_id         TEXT,
    input_tokens       INT,
    output_tokens      INT,
    cache_read_tokens  INT,
    cache_write_tokens INT,
    est_cost_usd       NUMERIC(12, 6) NOT NULL,   -- worst-case, reserved BEFORE the request
    actual_cost_usd    NUMERIC(12, 6),            -- from real usage, AFTER the response
    reconciled_console BOOLEAN     NOT NULL DEFAULT false,   -- set true once matched to the console
    started_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at        TIMESTAMPTZ,
    CONSTRAINT ck_pa_status CHECK (status IN ('started', 'completed', 'timeout', 'error', 'unknown')),
    CONSTRAINT ck_pa_est    CHECK (est_cost_usd >= 0),
    CONSTRAINT uq_paid_attempt UNIQUE (run_id, input_hash, attempt_no)
);
CREATE INDEX ix_pa_run    ON paid_attempts (run_id, started_at DESC);
CREATE INDEX ix_pa_window ON paid_attempts (started_at);          -- day/month budget windows
CREATE INDEX ix_pa_open   ON paid_attempts (status) WHERE status = 'started';   -- orphans to reconcile

COMMENT ON TABLE paid_attempts IS
    'Per-HTTP-attempt ledger for paid AI calls. Row written BEFORE the request (started) + finalized '
    'after (completed/timeout/error). Budget = sum over run/day/month. App: INSERT+UPDATE. Retention prunes.';

-- Operational (upsert/finalize needs UPDATE); app-role grants are applied per-table by migrate.py,
-- which adds paid_attempts to the mutable set. Retention may prune aged rows.
GRANT SELECT, DELETE ON paid_attempts TO trading_brain_retention;
