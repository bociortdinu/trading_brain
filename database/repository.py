"""Persistence for market snapshots.

Conflict policy for (symbol, bar_close):
- no existing row              -> INSERT              (status "inserted")
- same provider + pipeline_ver -> CONTROLLED ENRICH: fill NULL spread_pct / basis /
                                  data_quality only; never overwrite computed features
                                  (status "enriched" if something changed, else "unchanged")
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
    basis = Json(packet.basis_observed) if packet.basis_observed is not None else None
    dq = Json(packet.data_quality) if packet.data_quality is not None else None
    intervals = Json(packet.interval_list)

    with psycopg.connect(dsn) as conn:
        # 1. Concurrency-safe insert: the winner of a race inserts; everyone else falls through.
        #    The snapshot is a pure OBSERVATION; eligibility is persisted separately
        #    (snapshot_evaluations) so it never mutates the observation.
        row = conn.execute(
            """
            INSERT INTO market_snapshots
                (bar_close, symbol, regime, adx_h1, atr_pct_m15, spread_pct, features,
                 news_digest, provider, provider_symbol, ingested_at, intervals,
                 pipeline_version, data_quality, basis_observed)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (symbol, bar_close) DO NOTHING
            RETURNING id
            """,
            (
                packet.bar_close, packet.symbol, packet.regime, packet.adx_h1,
                packet.atr_pct_m15, packet.spread_pct, features, news, packet.provider,
                packet.provider_symbol, packet.ingested_at, intervals,
                packet.pipeline_version, dq, basis,
            ),
        ).fetchone()
        if row is not None:
            conn.commit()
            return "inserted", row[0]

        # 2. Existing row: lock it and decide enrich vs conflict.
        existing = conn.execute(
            "SELECT id, provider, pipeline_version, spread_pct, basis_observed, data_quality "
            "FROM market_snapshots WHERE symbol = %s AND bar_close = %s FOR UPDATE",
            (packet.symbol, packet.bar_close),
        ).fetchone()
        if existing is None:  # extremely rare: deleted between insert-conflict and select
            conn.rollback()
            return "conflict", None

        snap_id, provider, version, ex_spread, ex_basis, ex_dq = existing
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

        # Controlled enrichment: fill a NULL column only when we now have a value for it.
        fills: dict[str, object] = {}
        if ex_spread is None and packet.spread_pct is not None:
            fills["spread_pct"] = packet.spread_pct
        if ex_basis is None and basis is not None:
            fills["basis_observed"] = basis
        if ex_dq is None and dq is not None:
            fills["data_quality"] = dq
        if not fills:
            conn.rollback()
            return "unchanged", snap_id

        set_clause = ", ".join(f"{col} = %s" for col in fills)  # fixed column names, no injection
        conn.execute(
            f"UPDATE market_snapshots SET {set_clause} WHERE id = %s",
            (*fills.values(), snap_id),
        )
        conn.commit()
        return "enriched", snap_id


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
                    data_provider: str, tokens: dict | None = None) -> int:
    """Persist a DecisionRecord (decision/pipeline.py) with its reproducibility manifest and
    the FK to the authorizing evaluation. Never fabricates an approved verdict — the
    risk_verdict comes straight from the record."""
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
                 input_hash, as_of)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
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
                record.input_hash, record.as_of,
            ),
        ).fetchone()
        conn.commit()
    return row[0]


def open_shadow_trades(dsn: str, run_id: str) -> list[dict]:
    """OPEN shadow trades for a run — enough to reconstruct the VirtualTrade and reconcile
    them against new bars on a later tick."""
    import psycopg

    with psycopg.connect(dsn) as conn:
        rows = conn.execute(
            "SELECT decision_id, symbol, side, entry_price, sl_price, tp_price, opened_at, "
            "spread_pct, spread_provenance FROM trades "
            "WHERE mode = 'shadow' AND status = 'open' AND run_id = %s",
            (run_id,),
        ).fetchall()
    keys = ["decision_id", "symbol", "side", "entry_price", "sl_price", "tp_price",
            "opened_at", "spread_pct", "spread_provenance"]
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
    cost_model = costs or {
        "spread_pct": trade.spread_pct, "spread_provenance": trade.spread_provenance,
        "modeled": ["spread"], "not_modeled": ["commission", "swap", "slippage", "latency"],
    }
    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            """
            INSERT INTO trades
                (decision_id, run_id, symbol, side, mode, entry_price, sl_price, tp_price,
                 opened_at, status, exit_price, exit_reason, closed_at, r_multiple,
                 r_pessimistic, r_optimistic, ambiguous, timeframe, timeout_bars,
                 spread_pct, spread_provenance, costs)
            VALUES (%s,%s,%s,%s,'shadow',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
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
            RETURNING id, (xmax = 0) AS inserted
            """,
            (
                decision_id, run_id, symbol, side, trade.entry_mid, trade.sl_price, trade.tp_price,
                trade.opened_at, outcome.status, outcome.exit_price, outcome.exit_reason,
                outcome.closed_at, outcome.r_multiple, outcome.r_pessimistic, outcome.r_optimistic,
                outcome.ambiguous, timeframe, timeout_bars, trade.spread_pct,
                trade.spread_provenance, Json(cost_model),
            ),
        ).fetchone()
        conn.commit()
    trade_id, inserted = row
    return trade_id, ("inserted" if inserted else "updated")


def snapshot_enrichment_status(dsn: str, symbol: str, bar_close):
    """(exists, needs_spread_or_basis) for a stored snapshot — lets the scheduler
    retry XTB-spread enrichment only when it's actually missing."""
    import psycopg

    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            "SELECT spread_pct, basis_observed FROM market_snapshots "
            "WHERE symbol = %s AND bar_close = %s",
            (symbol, bar_close),
        ).fetchone()
    if row is None:
        return False, False
    spread, basis = row
    return True, (spread is None or basis is None)


def latest_snapshot_bar_close(dsn: str, symbol: str):
    """Most recent snapshot bar_close for `symbol`, or None. Used by the scheduler
    to detect missed bars on restart."""
    import psycopg

    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            "SELECT max(bar_close) FROM market_snapshots WHERE symbol = %s", (symbol,)
        ).fetchone()
    return row[0] if row and row[0] else None
