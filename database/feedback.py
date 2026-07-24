"""Feedback loop (Faza 5): what the shadow track record says, fed back into a later decision.

STRICT anti look-ahead: a decision at `as_of` may only see trades whose outcome was OBSERVED
before `as_of` — actually known by then. We gate on `outcome_observed_at` (when reconciliation
recorded the close), NOT `closed_at` (the bar the price hit): after a downtime a trade can close
at T but only be observed at T+downtime, and a decision in between must not see it. A row WITHOUT
`outcome_observed_at` is EXCLUDED (never assumed known at closed_at — that was a look-ahead risk);
the pre-0020 backtest rows were explicitly backfilled in migration 0022 (observed == closed there).

Two levels (level 3 kNN/pgvector is deferred):
- level 1: aggregate performance per market regime (small, cheap, always safe to include);
- level 2: the last K closed trades verbatim-ish (side, regime, R, exit) for pattern context.

Regime is the snapshot's H1 regime the decision was made on (trades -> decisions -> snapshots).
"""

from __future__ import annotations

from datetime import datetime


def regime_performance(dsn: str, *, run_id: str, before: datetime) -> list[dict]:
    """Per-regime performance over this run's shadow trades that CLOSED before `before`.
    Rows: {regime, trades, win_rate, expectancy_r}, ordered by trade count desc."""
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        return conn.execute(
            """
            SELECT s.regime AS regime,
                   count(*)                                              AS trades,
                   round(avg((t.r_multiple > 0)::int)::numeric, 3)       AS win_rate,
                   round(avg(t.r_multiple)::numeric, 3)                  AS expectancy_r
            FROM trades t
            JOIN decisions d       ON d.id = t.decision_id
            JOIN market_snapshots s ON s.id = d.snapshot_id
            WHERE t.run_id = %s
              AND t.status <> 'open'
              AND t.r_multiple IS NOT NULL
              AND t.outcome_observed_at IS NOT NULL   -- exclude rows w/o verified time provenance
              AND t.outcome_observed_at < %s
            GROUP BY s.regime
            ORDER BY trades DESC, regime
            """,
            (run_id, before),
        ).fetchall()


def recent_closed_trades(dsn: str, *, run_id: str, before: datetime, k: int = 5) -> list[dict]:
    """The last `k` shadow trades that CLOSED before `before`, most recent first.
    Compact per-trade context: side, regime, r_multiple, exit_reason, closed_at."""
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        return conn.execute(
            """
            SELECT t.side, s.regime, round(t.r_multiple::numeric, 3) AS r_multiple,
                   t.exit_reason, t.closed_at
            FROM trades t
            JOIN decisions d        ON d.id = t.decision_id
            JOIN market_snapshots s ON s.id = d.snapshot_id
            WHERE t.run_id = %s
              AND t.status <> 'open'
              AND t.r_multiple IS NOT NULL
              AND t.outcome_observed_at IS NOT NULL   -- exclude rows w/o verified time provenance
              AND t.outcome_observed_at < %s
            ORDER BY t.outcome_observed_at DESC
            LIMIT %s
            """,
            (run_id, before, k),
        ).fetchall()


def build_feedback(dsn: str | None, *, run_id: str | None, before: datetime, k: int = 5) -> dict:
    """Assemble the feedback context for a decision at `before`. Returns an empty-but-typed dict
    when there is no run/DB or no closed history yet (a fresh run legitimately has none)."""
    empty = {"regime_performance": [], "recent_trades": [], "as_of": before.isoformat()}
    if not dsn or not run_id:
        return empty
    regimes = regime_performance(dsn, run_id=run_id, before=before)
    recent = recent_closed_trades(dsn, run_id=run_id, before=before, k=k)
    # JSON-safe: numerics -> float, datetimes -> iso.
    for r in regimes:
        r["win_rate"] = float(r["win_rate"]) if r["win_rate"] is not None else None
        r["expectancy_r"] = float(r["expectancy_r"]) if r["expectancy_r"] is not None else None
    for r in recent:
        r["r_multiple"] = float(r["r_multiple"]) if r["r_multiple"] is not None else None
        r["closed_at"] = r["closed_at"].isoformat() if r["closed_at"] else None
    return {"regime_performance": regimes, "recent_trades": recent, "as_of": before.isoformat()}
