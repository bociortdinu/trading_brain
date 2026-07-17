-- 0019: stronger audit for a PAID API call.
--
-- llm_calls previously logged only an aggregated result with no record of how many transient
-- retries it took and no direct link to the decision it produced — you could see the cost but
-- not tie it to the decision, nor tell a one-shot success from one that retried three times.
--
-- retry_count: transient retries performed before the logged result (0 = succeeded first try).
-- decision_id: the decision this call produced (NULL for a failed call that yielded no decision).
--   ON DELETE SET NULL keeps the (append-only) cost record even if the decision is later purged.
ALTER TABLE llm_calls ADD COLUMN retry_count INT NOT NULL DEFAULT 0;
ALTER TABLE llm_calls ADD COLUMN decision_id BIGINT REFERENCES decisions (id) ON DELETE SET NULL;

CREATE INDEX ix_llm_calls_decision ON llm_calls (decision_id);
