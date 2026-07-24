-- 0009: audit log of EVERY LLM call (success AND failure), separate from `decisions`.
--
-- Before: a failed LLM call raised before insert_decision, so failures left NO record and
-- the DB kept no request_id / effective model / stop_reason / cache counts / cost / error
-- class. Under pay-per-token this is exactly what we must track. A failed call is not a
-- decision, so it belongs in its own table; a successful call links to its snapshot.

CREATE TABLE llm_calls (
    id                    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ts                    TIMESTAMPTZ NOT NULL DEFAULT now(),
    snapshot_id           BIGINT REFERENCES market_snapshots (id),
    ok                    BOOLEAN NOT NULL,
    error                 TEXT,            -- error class when ok = false (e.g. api_status:400)
    requested_model       TEXT NOT NULL,
    effective_model       TEXT,
    request_id            TEXT,
    stop_reason           TEXT,
    input_tokens          INT,
    output_tokens         INT,
    cache_read_tokens     INT,
    cache_creation_tokens INT,
    estimated_cost_usd    NUMERIC(12, 6),
    latency_ms            INT,
    prompt_version        TEXT,
    schema_version        TEXT,
    input_hash            TEXT
);
CREATE INDEX ix_llm_calls_snapshot ON llm_calls (snapshot_id);
CREATE INDEX ix_llm_calls_ok ON llm_calls (ok, ts DESC);
