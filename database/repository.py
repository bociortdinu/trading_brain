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

from contextlib import contextmanager

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
            ON CONFLICT (symbol, provider, provider_symbol, pipeline_version, bar_close) DO NOTHING
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

        # 2. SAME-SOURCE re-observation (identity = symbol+provider+provider_symbol+pipeline_version
        #    +bar_close): a DIFFERENT source (provider / instrument / pipeline) is now a distinct row
        #    (inserted above), never reaches here. Lock the existing row and only back-fill
        #    data_quality. The WHERE must match the uq_snap_source key exactly.
        existing = conn.execute(
            "SELECT id, data_quality FROM market_snapshots WHERE symbol=%s AND provider=%s "
            "AND provider_symbol=%s AND pipeline_version=%s AND bar_close=%s FOR UPDATE",
            (packet.symbol, packet.provider, packet.provider_symbol, packet.pipeline_version,
             packet.bar_close),
        ).fetchone()
        if existing is None:  # extremely rare: deleted between insert-conflict and select
            conn.rollback()
            return "conflict", None

        snap_id, ex_dq = existing
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


def _open_trade_row(conn, *, decision_id: int, run_id: str, symbol: str, trade, outcome,
                    timeframe: str, timeout_bars: int, costs: dict, observed_at):
    """INSERT a FRESH open (or immediately-closed) trade on an EXISTING connection — used to
    persist a decision and its trade in ONE transaction (online), so a crash can never leave a
    committed decision with no trade. Fresh decision_id -> no conflict is possible."""
    from psycopg.types.json import Json

    from core.models import Direction

    side = "buy" if trade.direction == Direction.BUY else "sell"
    observed = None if outcome.status == "open" else (observed_at or outcome.closed_at)
    conn.execute(
        """
        INSERT INTO trades
            (decision_id, run_id, symbol, side, mode, entry_price, sl_price, tp_price,
             opened_at, status, exit_price, exit_reason, closed_at, outcome_observed_at,
             r_multiple, r_pessimistic, r_optimistic, ambiguous, timeframe, timeout_bars,
             spread_pct, spread_provenance, slippage_pct, costs)
        VALUES (%s,%s,%s,%s,'shadow',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """,
        (decision_id, run_id, symbol, side, trade.entry_mid, trade.sl_price, trade.tp_price,
         trade.opened_at, outcome.status, outcome.exit_price, outcome.exit_reason, outcome.closed_at,
         observed, outcome.r_multiple, outcome.r_pessimistic, outcome.r_optimistic, outcome.ambiguous,
         timeframe, timeout_bars, trade.spread_pct, trade.spread_provenance, trade.slippage_pct,
         Json(costs)),
    )


def _llm_call_row(conn, result, *, snapshot_id: int | None, decision_id: int | None):
    """INSERT one llm_calls row on an EXISTING connection (no commit). Shared by insert_llm_call
    (standalone) and insert_decision (atomic with the decision), so a paid call is never persisted
    in a transaction separate from the decision it produced — an audit-insert failure would
    otherwise leave the decision committed and the paid call invisible."""
    conn.execute(
        """
        INSERT INTO llm_calls
            (snapshot_id, ok, error, requested_model, effective_model, request_id,
             stop_reason, input_tokens, output_tokens, cache_read_tokens,
             cache_creation_tokens, estimated_cost_usd, latency_ms, prompt_version,
             schema_version, input_hash, retry_count, decision_id)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """,
        (
            snapshot_id, result.ok, result.error, result.requested_model,
            result.effective_model, result.request_id, result.stop_reason,
            result.input_tokens, result.output_tokens, result.cache_read_input_tokens,
            result.cache_creation_input_tokens, result.estimated_cost_usd, result.latency_ms,
            result.prompt_version, result.schema_version, result.input_hash,
            result.retry_count, decision_id,
        ),
    )


def insert_decision(dsn: str, *, snapshot_id: int, evaluation_id: int | None, model: str,
                    record, ai_input: dict, ai_output: dict | None, mode: str,
                    data_provider: str, tokens: dict | None = None,
                    run_id: str | None = None, input_fingerprint: str | None = None,
                    spread_observation_id: int | None = None,
                    blocked_reason: str | None = None, llm_result=None,
                    open_trade: dict | None = None) -> tuple[int, bool]:
    """Persist a DecisionRecord (decision/pipeline.py) with its reproducibility manifest and
    the FK to the authorizing evaluation. Never fabricates an approved verdict — the
    risk_verdict comes straight from the record. Returns (decision_id, inserted): `inserted` is
    False when this (input_fingerprint, run_id) already existed — the caller then MUST treat the
    returned id as a PRE-EXISTING decision and not as the product of its own `record`.

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
                 input_hash, as_of, run_id, input_fingerprint, spread_observation_id,
                 blocked_reason)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
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
                spread_observation_id, blocked_reason,
            ),
        ).fetchone()
        inserted = row is not None
        if row is None:   # ON CONFLICT DO NOTHING -> the decision already exists for this run
            row = conn.execute(
                "SELECT id FROM decisions WHERE input_fingerprint = %s AND run_id = %s",
                (input_fingerprint, run_id),
            ).fetchone()
        # ATOMIC audit: the paid call is logged in the SAME transaction as the decision it
        # produced, only when we actually inserted (a conflict means an earlier attempt already
        # logged it). If this raises, the whole transaction rolls back — decision and audit stay
        # consistent, never a committed decision with a lost paid call.
        if llm_result is not None and inserted:
            _llm_call_row(conn, llm_result, snapshot_id=snapshot_id, decision_id=row[0])
        # Atomic decision -> trade (online): the open trade is written in the SAME transaction, so
        # a crash can't leave a committed decision without its trade. Only on a genuine insert.
        if open_trade is not None and inserted:
            _open_trade_row(conn, decision_id=row[0], run_id=run_id, **open_trade)
        conn.commit()
    # (id, inserted): a caller that hit a conflict must NOT proceed as if it produced this
    # decision — the row belongs to an earlier attempt and any fresh `rec` would be discarded.
    return row[0], inserted


class RunConfigMismatch(RuntimeError):
    """This run_id was first used with a DIFFERENT execution config. A run_id is one frozen setup."""


def assert_run_manifest(dsn: str, run_id: str, manifest: dict, manifest_hash: str) -> None:
    """Pin a run_id to ONE execution config. The first persisted use records the manifest; a later
    use with a different hash is REFUSED (pick a new run_id) so two configs can't be mixed into one
    experiment. Idempotent for the same config."""
    import psycopg
    from psycopg.types.json import Json

    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            "INSERT INTO run_manifests (run_id, manifest_hash, manifest) VALUES (%s,%s,%s) "
            "ON CONFLICT (run_id) DO NOTHING RETURNING manifest_hash",
            (run_id, manifest_hash, Json(manifest)),
        ).fetchone()
        if row is None:   # run_id already pinned -> its hash must match ours
            existing = conn.execute(
                "SELECT manifest_hash FROM run_manifests WHERE run_id = %s", (run_id,)).fetchone()[0]
            conn.commit()
            if existing != manifest_hash:
                raise RunConfigMismatch(
                    f"run_id {run_id!r} was pinned to execution config {existing[:12]}…, not "
                    f"{manifest_hash[:12]}… — use a NEW run_id for a different config")
            return
        conn.commit()


class RunLockedError(RuntimeError):
    """Another worker is already processing this run_id."""


@contextmanager
def run_lock(dsn: str, run_id: str):
    """EXCLUSIVE lock for the whole run. A backtest is STATEFUL: `busy_until` carries the open
    position forward, bar by bar. Per-bar reservations stop two workers from paying twice for the
    same bar, but they do NOT make the run parallelisable — two workers simply split the bars
    between them and each keeps its OWN busy_until, so both open positions and the
    "one position at a time" strategy silently stops holding (measured: 2 workers -> 19 trades,
    11 overlapping pairs). The single-position result is only meaningful if the run is serial.

    Fail-closed: refuse rather than wait, so a second worker cannot quietly corrupt an experiment.
    Session-scoped advisory lock, released when this connection closes.
    """
    import psycopg

    # autocommit: a session advisory lock does not need a transaction, and holding one open for
    # the entire backtest would show up as 'idle in transaction' the whole time (and could pin
    # vacuum). Each statement here stands alone.
    conn = psycopg.connect(dsn, autocommit=True)
    try:
        got = conn.execute("SELECT pg_try_advisory_lock(hashtext(%s)::bigint)", (run_id,)).fetchone()[0]
        if not got:
            raise RunLockedError(
                f"run_id {run_id!r} is already being processed by another worker. A stateful "
                f"backtest must run serially — parallel workers would each track their own open "
                f"position and produce overlapping trades."
            )
        yield
    finally:
        try:
            conn.execute("SELECT pg_advisory_unlock(hashtext(%s)::bigint)", (run_id,))
        finally:
            conn.close()


class StaleClaimError(RuntimeError):
    """Completion was attempted for a claim this worker no longer owns (its lease was taken
    over). Fail loudly: silently writing the result would corrupt the other worker's claim."""


def reserve_decision(dsn: str, *, input_fingerprint: str, run_id: str, worker: str,
                     lease_seconds: int = 300) -> tuple[str, str | None]:
    """Atomically claim the right to make (and PAY for) this decision. Call BEFORE the model.

    Returns (state, claim_token):
      ("reserved", token) — we own it; the ONLY worker allowed to call the model for this input.
                            `token` must be handed back to complete_decision_reservation.
      ("done", None)      — already decided in this run; skip (resume).
      ("held", None)      — another worker owns a live lease; it is paying, we must not.

    Atomic because the claim is a single INSERT ... ON CONFLICT: concurrent workers cannot both
    win. A dead worker's claim is reclaimable once its lease expires, and a 'failed' attempt may
    be retried; 'done' is terminal.

    The token is what makes ownership real. Without it, a worker whose lease expired could wake
    up and complete a claim now held by somebody else — stamping 'done' over live work and
    leaving an input decided-but-decisionless forever.

    HONEST GUARANTEE: this is AT-MOST-ONE CONCURRENT attempt, NOT exact-once. If a worker calls
    the model successfully and then dies BEFORE persisting, its lease eventually expires and a
    second worker will call the model AGAIN — a duplicate CHARGE. Closing that window needs a
    provider-accepted idempotency key on the API call itself, which we do not have. Do not claim
    "no duplicate charge".
    """
    import uuid

    import psycopg

    token = uuid.uuid4().hex
    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            """
            INSERT INTO decision_reservations
                (input_fingerprint, run_id, status, worker, claim_token, reserved_at, lease_expires_at)
            VALUES (%s, %s, 'in_progress', %s, %s, now(), now() + make_interval(secs => %s))
            ON CONFLICT (input_fingerprint, run_id) DO UPDATE SET
                worker           = EXCLUDED.worker,
                claim_token      = EXCLUDED.claim_token,
                reserved_at      = now(),
                lease_expires_at = EXCLUDED.lease_expires_at,
                status           = 'in_progress'
            WHERE decision_reservations.status = 'failed'
               OR (decision_reservations.status = 'in_progress'
                   AND decision_reservations.lease_expires_at < now())
            RETURNING claim_token
            """,
            (input_fingerprint, run_id, worker, token, lease_seconds),
        ).fetchone()
        if row is not None:
            conn.commit()
            return "reserved", row[0]
        existing = conn.execute(
            "SELECT status FROM decision_reservations WHERE input_fingerprint = %s AND run_id = %s",
            (input_fingerprint, run_id),
        ).fetchone()
        conn.commit()
    return ("done", None) if existing and existing[0] == "done" else ("held", None)


def complete_decision_reservation(dsn: str, *, input_fingerprint: str, run_id: str, status: str,
                                  claim_token: str, decision_id: int | None = None) -> None:
    """Close out a reservation WE own: 'done' (terminal) or 'failed' (retryable by any worker).

    Compare-and-set on the claim token: only the worker still holding the claim may close it. If
    the lease was taken over while we were working, the UPDATE matches nothing and we raise —
    better a loud failure than silently overwriting the new owner's claim.

    'done' means a COMPLETE chain, so it must carry the decision it produced. Enforced here as
    well as by the DB check (ck_reservation_done_has_decision): a 'done' with no decision would
    tell every later run "already decided" about a decision that does not exist.
    """
    import psycopg

    if status not in ("done", "failed"):
        raise ValueError(f"a reservation is completed as 'done' or 'failed', not {status!r}")
    if status == "done" and decision_id is None:
        raise ValueError("cannot mark a reservation 'done' without the decision it produced")

    with psycopg.connect(dsn) as conn:
        cur = conn.execute(
            "UPDATE decision_reservations SET status = %s, decision_id = %s "
            "WHERE input_fingerprint = %s AND run_id = %s AND claim_token = %s "
            "AND status = 'in_progress'",
            (status, decision_id, input_fingerprint, run_id, claim_token),
        )
        conn.commit()
        if cur.rowcount != 1:
            raise StaleClaimError(
                f"claim for {input_fingerprint[:12]}… in run {run_id!r} is no longer ours "
                f"(rows matched: {cur.rowcount}); another worker took the lease over"
            )


def load_decided_outcome(dsn: str, *, input_fingerprint: str, run_id: str) -> dict | None:
    """Everything a RESUME needs to reproduce what an earlier run already did for this input:
    the decision's direction/verdict and the trade it opened (if any), with its outcome.

    Without this a resumed backtest only knew "already decided" — it forgot any position that
    was still open, so it could stack a second entry on top and report a different run."""
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        row = conn.execute(
            """
            SELECT d.id AS decision_id, d.direction, d.risk_verdict, d.blocked_reason,
                   d.sl_pct, d.tp_pct, d.confidence, s.regime,
                   t.status, t.exit_reason, t.exit_price, t.closed_at, t.opened_at,
                   t.r_multiple, t.r_pessimistic, t.r_optimistic, t.ambiguous
            FROM decisions d
            JOIN market_snapshots s ON s.id = d.snapshot_id
            LEFT JOIN trades t ON t.decision_id = d.id AND t.run_id = d.run_id
            WHERE d.input_fingerprint = %s AND d.run_id = %s
            LIMIT 1
            """,
            (input_fingerprint, run_id),
        ).fetchone()
    return row


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


def open_shadow_trades(dsn: str, run_id: str | None = None, *, symbol: str | None = None) -> list[dict]:
    """OPEN shadow trades — enough to reconstruct the VirtualTrade and reconcile them against new
    bars on a later tick. Filter by `run_id` (one run) OR `symbol` (ACROSS runs, so a new run can
    still drain/reconcile positions left open by a previous run). Each row carries its own run_id."""
    import psycopg

    from psycopg.rows import dict_row

    where = ["t.mode = 'shadow'", "t.status = 'open'"]
    params: list = []
    if run_id is not None:
        where.append("t.run_id = %s")
        params.append(run_id)
    if symbol is not None:
        where.append("t.symbol = %s")
        params.append(symbol)
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        return conn.execute(
            "SELECT t.decision_id, t.run_id, t.symbol, t.side, t.entry_price, t.sl_price, "
            "t.tp_price, t.opened_at, t.spread_pct, t.spread_provenance, t.slippage_pct, "
            "t.timeout_bars, t.costs, d.data_provider "   # the FROZEN provider the trade was opened under
            "FROM trades t JOIN decisions d ON d.id = t.decision_id "
            "WHERE " + " AND ".join(where),
            tuple(params),
        ).fetchall()


def insert_llm_call(dsn: str, result, *, snapshot_id: int | None = None,
                    decision_id: int | None = None) -> int:
    """Audit-log one LLM call (success OR failure) with its full manifest + cost. `result`
    is a decision.llm_client.LlmCallResult. Persisting failures too means a failed/refused/
    rate-limited call is never invisible under pay-per-token. `retry_count` records how many
    transient retries preceded this result; `decision_id` links the (paid) call to the decision
    it produced (NULL for a failed call that yielded none)."""
    import psycopg

    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            """
            INSERT INTO llm_calls
                (snapshot_id, ok, error, requested_model, effective_model, request_id,
                 stop_reason, input_tokens, output_tokens, cache_read_tokens,
                 cache_creation_tokens, estimated_cost_usd, latency_ms, prompt_version,
                 schema_version, input_hash, retry_count, decision_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id
            """,
            (
                snapshot_id, result.ok, result.error, result.requested_model,
                result.effective_model, result.request_id, result.stop_reason,
                result.input_tokens, result.output_tokens, result.cache_read_input_tokens,
                result.cache_creation_input_tokens, result.estimated_cost_usd, result.latency_ms,
                result.prompt_version, result.schema_version, result.input_hash,
                result.retry_count, decision_id,
            ),
        ).fetchone()
        conn.commit()
    return row[0]


def upsert_shadow_trade(dsn: str, *, decision_id: int, run_id: str, symbol: str, trade, outcome,
                        timeframe: str, timeout_bars: int, costs: dict | None = None,
                        observed_at=None) -> tuple[int, str]:
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
    # WHEN the outcome became known: caller-supplied (online = reconcile wall-clock) or, for a
    # deterministic backtest, the close time itself (no observation lag). NULL while still open.
    observed = None if outcome.status == "open" else (observed_at or outcome.closed_at)
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
                 opened_at, status, exit_price, exit_reason, closed_at, outcome_observed_at,
                 r_multiple, r_pessimistic, r_optimistic, ambiguous, timeframe, timeout_bars,
                 spread_pct, spread_provenance, slippage_pct, costs)
            VALUES (%s,%s,%s,%s,'shadow',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (decision_id, run_id) DO UPDATE SET
                status              = EXCLUDED.status,
                exit_price          = EXCLUDED.exit_price,
                exit_reason         = EXCLUDED.exit_reason,
                closed_at           = EXCLUDED.closed_at,
                outcome_observed_at = EXCLUDED.outcome_observed_at,
                r_multiple          = EXCLUDED.r_multiple,
                r_pessimistic       = EXCLUDED.r_pessimistic,
                r_optimistic        = EXCLUDED.r_optimistic,
                ambiguous           = EXCLUDED.ambiguous,
                costs               = EXCLUDED.costs
            WHERE trades.status = 'open'
            RETURNING id, (xmax = 0) AS inserted
            """,
            (
                decision_id, run_id, symbol, side, trade.entry_mid, trade.sl_price, trade.tp_price,
                trade.opened_at, outcome.status, outcome.exit_price, outcome.exit_reason,
                outcome.closed_at, observed, outcome.r_multiple, outcome.r_pessimistic,
                outcome.r_optimistic, outcome.ambiguous, timeframe, timeout_bars, trade.spread_pct,
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


def snapshot_spread_status(dsn: str, symbol: str, bar_close,
                           provider: str | None = None) -> tuple[bool, bool]:
    """(snapshot_exists, has_no_spread_observation) — lets the ONLINE scheduler retry the XTB
    quote only for a bar that still has no contextual spread recorded. The snapshot itself is
    never mutated; a retry appends a new spread_observations row.

    SOURCE-SCOPED: since a bar can now be observed by several providers (0024/0025), the caller
    passes the CURRENT `provider` so we answer for THAT source's snapshot, never another feed's.
    When two same-source rows exist for a bar (e.g. a pipeline upgrade), the most recent wins."""
    import psycopg

    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            "SELECT s.id, EXISTS (SELECT 1 FROM spread_observations o WHERE o.snapshot_id = s.id) "
            "FROM market_snapshots s WHERE s.symbol = %s AND s.bar_close = %s "
            "AND (%s::text IS NULL OR s.provider = %s) ORDER BY s.id DESC LIMIT 1",
            (symbol, bar_close, provider, provider),
        ).fetchone()
    if row is None:
        return False, False
    return True, not row[1]


def latest_snapshot_bar_close(dsn: str, symbol: str, provider: str | None = None):
    """Most recent snapshot bar_close for `symbol` (optionally for one `provider` — a snapshot is
    now source-specific, so mixing providers here would skip bars). Used to detect missed bars."""
    import psycopg

    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            "SELECT max(bar_close) FROM market_snapshots "
            "WHERE symbol = %s AND (%s::text IS NULL OR provider = %s)",
            (symbol, provider, provider),
        ).fetchone()
    return row[0] if row and row[0] else None


def last_decision_as_of(dsn: str, run_id: str, symbol: str, provider: str | None = None):
    """The as_of of the most recent decision for this run+symbol (optionally one `provider`) — the
    bar the online loop last decided on. Used to detect/log how many bars a downtime gap skipped."""
    import psycopg

    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            "SELECT max(s.bar_close) FROM decisions d JOIN market_snapshots s ON s.id=d.snapshot_id "
            "WHERE d.run_id = %s AND s.symbol = %s AND (%s::text IS NULL OR s.provider = %s)",
            (run_id, symbol, provider, provider),
        ).fetchone()
    return row[0] if row and row[0] else None


def last_decision_bar_across_runs(dsn: str, symbol: str, provider: str | None = None):
    """(bar_close, run_id) of the most recent decision for this symbol+provider ACROSS ALL RUNS —
    NOT scoped to the current run. Downtime-gap detection uses this so a gap that spans a run/config
    change is still seen (continuity): a new run_id must not reset the 'last decided bar' to nothing
    and thereby hide the gap. Returns (None, None) if there is no prior decision at all."""
    import psycopg

    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            "SELECT s.bar_close, d.run_id "
            "FROM decisions d JOIN market_snapshots s ON s.id=d.snapshot_id "
            "WHERE s.symbol = %s AND (%s::text IS NULL OR s.provider = %s) "
            "ORDER BY s.bar_close DESC LIMIT 1",
            (symbol, provider, provider),
        ).fetchone()
    return (row[0], row[1]) if row else (None, None)


def record_downtime_gap(dsn: str, *, symbol: str, provider: str, prev_bar_close, prev_run_id,
                        resumed_bar_close, run_id: str, missed_bars: int, policy: str) -> int | None:
    """Persist a downtime gap as an AUDITABLE FACT (append-only; not just a log line): how many
    open-market decision bars were skipped between the last decided bar and the resume bar, which
    runs sat on either side of the gap (prev_run_id may differ from run_id — a gap across a config
    change), and how it was handled (`policy`: 'skip' | 'backfill'). Idempotent on
    (symbol, provider, resumed_bar_close, run_id): re-ticking the same resume never duplicates it.
    Returns the row id, or None if the gap was already recorded."""
    import psycopg

    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            """
            INSERT INTO downtime_gaps
                (symbol, provider, prev_bar_close, prev_run_id, resumed_bar_close, run_id,
                 missed_bars, policy)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (symbol, provider, resumed_bar_close, run_id) DO NOTHING
            RETURNING id
            """,
            (symbol, provider, prev_bar_close, prev_run_id, resumed_bar_close, run_id,
             missed_bars, policy),
        ).fetchone()
        conn.commit()
    return row[0] if row else None
