"""Persistence for market snapshots.

A snapshot is an IMMUTABLE observation of a CLOSED bar: OHLCV-derived features for
(symbol, bar_close). It deliberately carries NO spread — a spread comes from a live quote at
some observation instant, which is NOT a property of the bar. Contextual spread lives in
`spread_observations` (append-only, migration 0013) and a decision records exactly which
observation it consumed. That way a snapshot can never assert a spread the decision never used.

Conflict policy for (symbol, bar_close):
- no existing row              -> INSERT              (status "inserted")
- same provider + pipeline_ver -> fill a NULL data_quality only; NEVER overwrite computed
                                  features (status "enriched" if it changed, else "unchanged")
- different provider/version   -> persisted to snapshot_conflicts and reported
                                  (status "conflict"); never silently dropped / overwritten

Concurrency-safe: uses INSERT ... ON CONFLICT DO NOTHING, then SELECT ... FOR UPDATE
on the existing row, so racing inserts don't raise a unique violation.
"""

from __future__ import annotations

from features.mtf import FeaturePacket


def upsert_snapshot(dsn: str, packet: FeaturePacket) -> tuple[str, int | None]:
    import psycopg
    from psycopg.types.json import Json

    features = Json(packet.features_json())
    news = Json(packet.news_digest) if packet.news_digest is not None else None
    dq = Json(packet.data_quality) if packet.data_quality is not None else None
    intervals = Json(packet.interval_list)

    with psycopg.connect(dsn) as conn:
        # 1. Concurrency-safe insert: the winner of a race inserts; everyone else falls through.
        #    The snapshot is a pure OBSERVATION; eligibility (snapshot_evaluations) and the
        #    contextual spread (spread_observations) are persisted separately so neither ever
        #    mutates the observation.
        row = conn.execute(
            """
            INSERT INTO market_snapshots
                (bar_close, symbol, regime, adx_h1, atr_pct_m15, features,
                 news_digest, provider, provider_symbol, ingested_at, intervals,
                 pipeline_version, data_quality)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (symbol, bar_close) DO NOTHING
            RETURNING id
            """,
            (
                packet.bar_close, packet.symbol, packet.regime, packet.adx_h1,
                packet.atr_pct_m15, features, news, packet.provider,
                packet.provider_symbol, packet.ingested_at, intervals,
                packet.pipeline_version, dq,
            ),
        ).fetchone()
        if row is not None:
            conn.commit()
            return "inserted", row[0]

        # 2. Existing row: lock it and decide enrich vs conflict.
        existing = conn.execute(
            "SELECT id, provider, pipeline_version, data_quality "
            "FROM market_snapshots WHERE symbol = %s AND bar_close = %s FOR UPDATE",
            (packet.symbol, packet.bar_close),
        ).fetchone()
        if existing is None:  # extremely rare: deleted between insert-conflict and select
            conn.rollback()
            return "conflict", None

        snap_id, provider, version, ex_dq = existing
        if provider != packet.provider or version != packet.pipeline_version:
            conn.execute(
                """
                INSERT INTO snapshot_conflicts
                    (symbol, bar_close, existing_snapshot_id, existing_provider,
                     existing_pipeline_version, incoming_provider, incoming_pipeline_version)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                """,
                (packet.symbol, packet.bar_close, snap_id, provider, version,
                 packet.provider, packet.pipeline_version),
            )
            conn.commit()
            return "conflict", snap_id

        # Only data_quality may be back-filled (and only when it is still NULL). Features are
        # never touched; the spread is not here at all any more.
        if ex_dq is not None or dq is None:
            conn.rollback()
            return "unchanged", snap_id
        conn.execute("UPDATE market_snapshots SET data_quality = %s WHERE id = %s", (dq, snap_id))
        conn.commit()
        return "enriched", snap_id


def insert_spread_observation(dsn: str, *, snapshot_id: int, spread_pct: float, provenance: str,
                              observed_at, quote_time=None, basis: dict | None = None) -> int:
    """Append a CONTEXTUAL spread fact about a snapshot: 'at `observed_at`, with this provenance,
    the spread was X'. Append-only and idempotent on (snapshot_id, provenance, observed_at) — the
    snapshot itself is never mutated, so a replay decision can't inherit an online quote."""
    import psycopg
    from psycopg.types.json import Json

    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            """
            INSERT INTO spread_observations
                (snapshot_id, spread_pct, provenance, quote_time, observed_at, basis)
            VALUES (%s,%s,%s,%s,%s,%s)
            ON CONFLICT (snapshot_id, provenance, observed_at) DO NOTHING
            RETURNING id
            """,
            (snapshot_id, spread_pct, provenance, quote_time, observed_at,
             Json(basis) if basis is not None else None),
        ).fetchone()
        if row is None:   # already recorded -> return the existing observation
            row = conn.execute(
                "SELECT id FROM spread_observations WHERE snapshot_id = %s AND provenance = %s "
                "AND observed_at = %s",
                (snapshot_id, provenance, observed_at),
            ).fetchone()
        conn.commit()
    return row[0]


def insert_evaluation(dsn: str, snapshot_id: int, result) -> int:
    """APPEND one eligibility verdict (immutable) and return its id.

    Append-only: every verdict is a new row, so a decision can reference the EXACT frozen
    verdict that authorized it and that verdict never changes. Online and replay are distinct
    rows; a later re-evaluation of the same (mode, policy) is a NEW row (the latest by
    evaluated_at is 'current'). `result` is a features.eligibility.EligibilityResult.
    """
    import psycopg
    from psycopg.types.json import Json

    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            """
            INSERT INTO snapshot_evaluations
                (snapshot_id, mode, policy_version, eligible, reasons, policy,
                 evaluated_at, ref_now, feed_lag_seconds, quote_lag_seconds, quote_present)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id
            """,
            (
                snapshot_id, result.mode, result.policy_version, result.eligible,
                Json(result.reasons), Json(result.policy), result.evaluated_at,
                result.ref_now, result.feed_lag_seconds, result.quote_lag_seconds,
                result.quote_present,
            ),
        ).fetchone()
        conn.commit()
    return row[0]


def latest_evaluation_id(dsn: str, snapshot_id: int, mode: str, policy_version: str) -> int | None:
    """Id of the most recent verdict for (snapshot, mode, policy) — the 'current' one."""
    import psycopg

    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            "SELECT id FROM snapshot_evaluations "
            "WHERE snapshot_id = %s AND mode = %s AND policy_version = %s "
            "ORDER BY evaluated_at DESC LIMIT 1",
            (snapshot_id, mode, policy_version),
        ).fetchone()
    return row[0] if row else None


def evaluations_for(dsn: str, snapshot_id: int) -> list[dict]:
    """All eligibility verdicts recorded for a snapshot (for audit/tests), newest first."""
    import psycopg

    with psycopg.connect(dsn) as conn:
        rows = conn.execute(
            "SELECT id, mode, policy_version, eligible, reasons, quote_present "
            "FROM snapshot_evaluations WHERE snapshot_id = %s ORDER BY evaluated_at DESC",
            (snapshot_id,),
        ).fetchall()
    return [
        {"id": i, "mode": m, "policy_version": pv, "eligible": e, "reasons": r, "quote_present": qp}
        for (i, m, pv, e, r, qp) in rows
    ]


def insert_decision(dsn: str, *, snapshot_id: int, evaluation_id: int | None, model: str,
                    record, ai_input: dict, ai_output: dict | None, mode: str,
                    data_provider: str, tokens: dict | None = None,
                    run_id: str | None = None, input_fingerprint: str | None = None,
                    spread_observation_id: int | None = None) -> int:
    """Persist a DecisionRecord (decision/pipeline.py) with its reproducibility manifest and
    the FK to the authorizing evaluation. Never fabricates an approved verdict — the
    risk_verdict comes straight from the record.

    When `run_id` + `input_fingerprint` are given (shadow experiments), the insert is ATOMIC and
    idempotent: ON CONFLICT (input_fingerprint, run_id) DO NOTHING, so two concurrent inserts
    cannot both create a row; on conflict the EXISTING decision id is returned. run_id=None
    (app/decide, live) is unconstrained (partial index).

    `spread_observation_id` records EXACTLY which contextual spread the decision consumed
    (NULL = a modeled/replay constant, which `ai_input` records). The snapshot FK carries only
    the immutable OHLCV observation, so the chain reproduces the frozen input without conflict."""
    import psycopg
    from psycopg.types.json import Json

    risk = record.risk
    manifest = record.manifest
    approved = bool(risk and risk.approved)
    direction = record.decision.direction.value if record.decision else "NO_TRADE"
    tokens = tokens or {}

    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            """
            INSERT INTO decisions
                (snapshot_id, evaluation_id, model, direction, confidence, sl_pct, tp_pct,
                 risk_verdict, risk_reason, ai_input, ai_output, prompt_tokens, output_tokens,
                 latency_ms, cache_hit, mode, prompt_version, output_schema_version,
                 feature_pipeline_version, strategy_version, risk_config_version, data_provider,
                 input_hash, as_of, run_id, input_fingerprint, spread_observation_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (input_fingerprint, run_id)
                WHERE run_id IS NOT NULL AND input_fingerprint IS NOT NULL DO NOTHING
            RETURNING id
            """,
            (
                snapshot_id, evaluation_id, model, direction,
                record.decision.confidence if record.decision else 0.0,
                risk.sl_pct if risk else None, risk.tp_pct if risk else None,
                "approved" if approved else "rejected",
                (risk.reason if risk else "prefiltered_out"),
                Json(ai_input), Json(ai_output if ai_output is not None else {}),
                tokens.get("input"), tokens.get("output"), tokens.get("latency_ms"),
                tokens.get("cache_hit"), mode,
                manifest.get("prompt_version"), manifest.get("output_schema_version"),
                manifest.get("feature_pipeline_version"), manifest.get("strategy_version"),
                manifest.get("risk_config_version"), data_provider,
                record.input_hash, record.as_of, run_id, input_fingerprint,
                spread_observation_id,
            ),
        ).fetchone()
        if row is None:   # ON CONFLICT DO NOTHING -> the decision already exists for this run
            row = conn.execute(
                "SELECT id FROM decisions WHERE input_fingerprint = %s AND run_id = %s",
                (input_fingerprint, run_id),
            ).fetchone()
        conn.commit()
    return row[0]


def find_decision_by_fingerprint(dsn: str, *, input_fingerprint: str, run_id: str) -> int | None:
    """Dedupe-BEFORE-the-LLM key: has this frozen input already been decided in this run? Lets a
    re-run RESUME without re-calling (re-paying) the model. Returns the decision id or None."""
    import psycopg

    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            "SELECT id FROM decisions WHERE input_fingerprint = %s AND run_id = %s LIMIT 1",
            (input_fingerprint, run_id),
        ).fetchone()
    return row[0] if row else None


def open_shadow_trades(dsn: str, run_id: str) -> list[dict]:
    """OPEN shadow trades for a run — enough to reconstruct the VirtualTrade and reconcile
    them against new bars on a later tick."""
    import psycopg

    with psycopg.connect(dsn) as conn:
        rows = conn.execute(
            "SELECT decision_id, symbol, side, entry_price, sl_price, tp_price, opened_at, "
            "spread_pct, spread_provenance, slippage_pct FROM trades "
            "WHERE mode = 'shadow' AND status = 'open' AND run_id = %s",
            (run_id,),
        ).fetchall()
    keys = ["decision_id", "symbol", "side", "entry_price", "sl_price", "tp_price",
            "opened_at", "spread_pct", "spread_provenance", "slippage_pct"]
    return [dict(zip(keys, r)) for r in rows]


def insert_llm_call(dsn: str, result, *, snapshot_id: int | None = None) -> int:
    """Audit-log one LLM call (success OR failure) with its full manifest + cost. `result`
    is a decision.llm_client.LlmCallResult. Persisting failures too means a failed/refused/
    rate-limited call is never invisible under pay-per-token."""
    import psycopg

    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            """
            INSERT INTO llm_calls
                (snapshot_id, ok, error, requested_model, effective_model, request_id,
                 stop_reason, input_tokens, output_tokens, cache_read_tokens,
                 cache_creation_tokens, estimated_cost_usd, latency_ms, prompt_version,
                 schema_version, input_hash)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id
            """,
            (
                snapshot_id, result.ok, result.error, result.requested_model,
                result.effective_model, result.request_id, result.stop_reason,
                result.input_tokens, result.output_tokens, result.cache_read_input_tokens,
                result.cache_creation_input_tokens, result.estimated_cost_usd, result.latency_ms,
                result.prompt_version, result.schema_version, result.input_hash,
            ),
        ).fetchone()
        conn.commit()
    return row[0]


def upsert_shadow_trade(dsn: str, *, decision_id: int, run_id: str, symbol: str, trade, outcome,
                        timeframe: str, timeout_bars: int, costs: dict | None = None) -> tuple[int, str]:
    """Idempotently persist/refresh a shadow trade for (decision_id, run_id).

    A re-run UPSERTs the SAME row — an open trade is closed IN PLACE (entry/SL/TP stay
    fixed; only the outcome + costs update), reconciliation is repeatable after a restart,
    and experiments are separated by run_id. Returns (id, "inserted"|"updated"). PnL is
    modeled; R-multiple is the primary metric with pessimistic/optimistic bands + ambiguity.
    `trade` is a shadow.virtual_broker.VirtualTrade, `outcome` a shadow.reconciler.Outcome.
    """
    import psycopg
    from psycopg.types.json import Json

    from core.models import Direction

    side = "buy" if trade.direction == Direction.BUY else "sell"
    # Prefer an explicit manifest from the caller (shadow.virtual_broker.cost_manifest, which
    # partitions modeled/not_modeled by ACTUAL non-zero rates). The fallback here is honest too:
    # with no config in scope, commission/swap rates are unknown -> not_modeled, never claimed.
    cost_model = costs or {
        "spread_pct": trade.spread_pct, "spread_provenance": trade.spread_provenance,
        "slippage_pct": trade.slippage_pct,
        "modeled": ["spread", "gap_through_stop", "latency"]
        + (["slippage"] if trade.slippage_pct != 0 else []),
        "not_modeled": ["commission", "swap"]
        + ([] if trade.slippage_pct != 0 else ["slippage"]),
        "note": "commission/swap rate unknown here (no config) -> NOT net of real financing",
    }
    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            """
            INSERT INTO trades
                (decision_id, run_id, symbol, side, mode, entry_price, sl_price, tp_price,
                 opened_at, status, exit_price, exit_reason, closed_at, r_multiple,
                 r_pessimistic, r_optimistic, ambiguous, timeframe, timeout_bars,
                 spread_pct, spread_provenance, slippage_pct, costs)
            VALUES (%s,%s,%s,%s,'shadow',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (decision_id, run_id) DO UPDATE SET
                status        = EXCLUDED.status,
                exit_price    = EXCLUDED.exit_price,
                exit_reason   = EXCLUDED.exit_reason,
                closed_at     = EXCLUDED.closed_at,
                r_multiple    = EXCLUDED.r_multiple,
                r_pessimistic = EXCLUDED.r_pessimistic,
                r_optimistic  = EXCLUDED.r_optimistic,
                ambiguous     = EXCLUDED.ambiguous,
                costs         = EXCLUDED.costs
            WHERE trades.status = 'open'
            RETURNING id, (xmax = 0) AS inserted
            """,
            (
                decision_id, run_id, symbol, side, trade.entry_mid, trade.sl_price, trade.tp_price,
                trade.opened_at, outcome.status, outcome.exit_price, outcome.exit_reason,
                outcome.closed_at, outcome.r_multiple, outcome.r_pessimistic, outcome.r_optimistic,
                outcome.ambiguous, timeframe, timeout_bars, trade.spread_pct,
                trade.spread_provenance, trade.slippage_pct, Json(cost_model),
            ),
        ).fetchone()
        # MONOTONE: `WHERE trades.status = 'open'` means a conflict on an ALREADY-CLOSED trade
        # updates nothing and RETURNING yields no row. A closed shadow trade is terminal — never
        # reopened or re-scored. Report it as unchanged instead of crashing on the empty result.
        if row is None:
            existing = conn.execute(
                "SELECT id FROM trades WHERE decision_id = %s AND run_id = %s",
                (decision_id, run_id),
            ).fetchone()
            conn.commit()
            return existing[0], "unchanged"
        conn.commit()
    trade_id, inserted = row
    return trade_id, ("inserted" if inserted else "updated")


def find_shadow_trade_by_input(dsn: str, *, input_hash: str, model: str, run_id: str) -> int | None:
    """End-to-end idempotency key: a shadow trade is uniquely the outcome of deciding a given
    FROZEN input (input_hash) with a given model, within an experiment (run_id). The DB's
    UNIQUE(decision_id, run_id) can't dedupe across re-runs because each re-run mints a NEW
    decision_id — so we dedupe on the input instead. Returns the existing trade id or None."""
    import psycopg

    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            "SELECT t.id FROM trades t JOIN decisions d ON t.decision_id = d.id "
            "WHERE d.input_hash = %s AND d.model = %s AND t.run_id = %s LIMIT 1",
            (input_hash, model, run_id),
        ).fetchone()
    return row[0] if row else None


def snapshot_spread_status(dsn: str, symbol: str, bar_close) -> tuple[bool, bool]:
    """(snapshot_exists, has_no_spread_observation) — lets the ONLINE scheduler retry the XTB
    quote only for a bar that still has no contextual spread recorded. The snapshot itself is
    never mutated; a retry appends a new spread_observations row."""
    import psycopg

    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            "SELECT s.id, EXISTS (SELECT 1 FROM spread_observations o WHERE o.snapshot_id = s.id) "
            "FROM market_snapshots s WHERE s.symbol = %s AND s.bar_close = %s",
            (symbol, bar_close),
        ).fetchone()
    if row is None:
        return False, False
    return True, not row[1]


def latest_snapshot_bar_close(dsn: str, symbol: str):
    """Most recent snapshot bar_close for `symbol`, or None. Used by the scheduler
    to detect missed bars on restart."""
    import psycopg

    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            "SELECT max(bar_close) FROM market_snapshots WHERE symbol = %s", (symbol,)
        ).fetchone()
    return row[0] if row and row[0] else None
