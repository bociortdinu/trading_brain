"""Shadow performance metrics: does the strategy have ANY edge, net of modeled costs?

All R-multiples are already NET of the round-trip spread (see reconciler). The both-hit
ambiguity is surfaced as a BAND: `avg_r_pessimistic` (stop-first on every ambiguous trade)
vs `avg_r_optimistic` (target-first) — the true expectancy lies between them. A strategy
whose edge only survives the optimistic end of the band is not a real edge.
"""

from __future__ import annotations

from statistics import mean


def summarize(rows: list[dict]) -> dict:
    """`rows` are dicts with keys: status, exit_reason, r_multiple, r_pessimistic,
    r_optimistic, ambiguous. Open trades (no r_multiple) are ignored for expectancy."""
    closed = [r for r in rows if r.get("r_multiple") is not None]
    n = len(closed)
    summary = {
        "trades_total": len(rows),
        "trades_closed": n,
        "trades_open": len(rows) - n,
    }
    if n == 0:
        return summary

    rs = [float(r["r_multiple"]) for r in closed]
    ambiguous = [r for r in closed if r.get("ambiguous")]
    # For the band, use each trade's pessimistic/optimistic R (== r_multiple when unambiguous).
    pess = [float(r["r_pessimistic"]) if r.get("r_pessimistic") is not None else float(r["r_multiple"]) for r in closed]
    opt = [float(r["r_optimistic"]) if r.get("r_optimistic") is not None else float(r["r_multiple"]) for r in closed]

    reasons: dict[str, int] = {}
    for r in closed:
        reasons[r.get("exit_reason") or "unknown"] = reasons.get(r.get("exit_reason") or "unknown", 0) + 1

    summary.update({
        "win_rate": round(sum(1 for r in rs if r > 0) / n, 3),
        "expectancy_r": round(mean(rs), 3),        # avg R per trade (the headline edge, net of cost)
        "total_r": round(sum(rs), 3),
        "best_r": round(max(rs), 3),
        "worst_r": round(min(rs), 3),
        "ambiguity_rate": round(len(ambiguous) / n, 3),
        "avg_r_pessimistic": round(mean(pess), 3),  # band low: stop-first on every ambiguous bar
        "avg_r_optimistic": round(mean(opt), 3),    # band high: target-first
        "exit_reasons": reasons,
    })
    return summary


def shadow_trade_rows(dsn: str, symbol: str | None = None) -> list[dict]:
    """Read shadow trades from the DB for summarize()."""
    import psycopg

    sql = ("SELECT status, exit_reason, r_multiple, r_pessimistic, r_optimistic, ambiguous "
           "FROM trades WHERE mode = 'shadow'")
    params: tuple = ()
    if symbol is not None:
        sql += " AND symbol = %s"
        params = (symbol,)
    with psycopg.connect(dsn) as conn:
        rows = conn.execute(sql, params).fetchall()
    keys = ["status", "exit_reason", "r_multiple", "r_pessimistic", "r_optimistic", "ambiguous"]
    return [dict(zip(keys, r)) for r in rows]
