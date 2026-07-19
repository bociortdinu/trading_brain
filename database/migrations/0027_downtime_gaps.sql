-- 0027: downtime gaps as a PERSISTENT, auditable fact.
--
-- Each online tick decides only the LATEST closed bar, so after downtime the open-market bars
-- between the last decision and now are skipped. Previously this was only a WARNING log line, and
-- it was measured against the last decision OF THE CURRENT RUN — so a gap that spanned a run/config
-- change (a new run_id) was invisible. This table records the gap as a fact: how many bars were
-- skipped (calendar-aware), which runs sat on either side (continuity across a run change), and how
-- the gap was handled (policy). The track record is "continuous" only if this table is empty.
CREATE TABLE downtime_gaps (
    id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    symbol            TEXT        NOT NULL,
    provider          TEXT        NOT NULL,
    prev_bar_close    TIMESTAMPTZ,             -- last DECIDED bar before the gap (NULL = cold start)
    prev_run_id       TEXT,                    -- run that made that prior decision (may != run_id:
                                               -- a gap ACROSS a config/run change)
    resumed_bar_close TIMESTAMPTZ NOT NULL,    -- the bar decision resumed on
    run_id            TEXT        NOT NULL,     -- run that OBSERVED + recorded the gap
    missed_bars       INT         NOT NULL,     -- open-market decision bars skipped (calendar-aware)
    policy            TEXT        NOT NULL,      -- how it was handled: 'skip' | 'backfill'
    detected_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_gap_missed CHECK (missed_bars >= 0),
    CONSTRAINT ck_gap_policy CHECK (policy IN ('skip', 'backfill')),
    -- Idempotent: re-ticking the same resume must not duplicate the gap fact.
    CONSTRAINT uq_gap UNIQUE (symbol, provider, resumed_bar_close, run_id)
);
CREATE INDEX ix_gap_symbol_provider ON downtime_gaps (symbol, provider, resumed_bar_close DESC);

COMMENT ON TABLE downtime_gaps IS
    'APPEND-ONLY fact: an audited downtime gap (open-market decision bars skipped). App role: '
    'INSERT only. Prune via trading_brain_retention.';

-- Append-only for the app role (migrate.py baseline grants SELECT+INSERT). Retention may prune.
GRANT SELECT, DELETE ON downtime_gaps TO trading_brain_retention;
