"""DB-gated tests for the read-only dashboard service: they exercise the REAL queries in
DashboardService.state() against the isolated `_test` database.

The headline regression: a QUARANTINED trade was never reconciled, so it must never be counted as
closed (that inflated trades_closed and corrupted the win-rate denominator)."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

from config.settings import Settings
from dashboard.service import DashboardService

psycopg = pytest.importorskip("psycopg")
DSN = os.environ.get("BRAIN_TEST_DB_DSN")


def _db_ok() -> bool:
    if not DSN:
        return False
    try:
        with psycopg.connect(DSN, connect_timeout=2):
            return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _db_ok(), reason="no reachable BRAIN_TEST_DB_DSN (..._test)")
UTC = timezone.utc


def _service() -> DashboardService:
    return DashboardService(Settings(db_dsn=DSN))


def _cleanup_dsn() -> str:
    return os.environ.get("BRAIN_TEST_ADMIN_DB_DSN") or DSN


def _seed_trade(sym, run_id, *, status, r=None, side="buy"):
    """snapshot -> evaluation -> decision -> trade in the requested terminal state."""
    from core.models import Direction
    from database.repository import quarantine_shadow_trade, upsert_shadow_trade
    from shadow.reconciler import reconcile
    from shadow.virtual_broker import ShadowConfig, cost_manifest, open_virtual_trade
    from tests.test_repository import _seed_decision

    cfg = ShadowConfig()
    end = datetime(2026, 7, 1, tzinfo=UTC)
    dec_id = _seed_decision(sym, run_id=run_id, input_fingerprint=f"fp-{os.urandom(3).hex()}")
    trade = open_virtual_trade(Direction.BUY if side == "buy" else Direction.SELL, 4000.0, 0.3, 0.6,
                               spread_pct=0.0, spread_provenance="modeled", opened_at=end)
    upsert_shadow_trade(DSN, decision_id=dec_id, run_id=run_id, symbol=sym, trade=trade,
                        outcome=reconcile(trade, [], cfg), timeframe="15min", timeout_bars=96,
                        costs=cost_manifest(trade, cfg))
    if status == "quarantined":
        quarantine_shadow_trade(DSN, decision_id=dec_id, run_id=run_id, reason="test quarantine")
    elif status in ("closed", "expired"):
        # Force the terminal state + R directly (admin): the point is the METRIC, not the reconciler.
        with psycopg.connect(_cleanup_dsn()) as c:
            c.execute("UPDATE trades SET status=%s, closed_at=now(), exit_price=4010, "
                      "exit_reason=%s, r_multiple=%s WHERE decision_id=%s",
                      (status, "tp_hit" if (r or 0) > 0 else "sl_hit", r, dec_id))
            c.commit()
    return dec_id


def _purge(sym, run_id):
    from tests.test_repository import _cleanup
    with psycopg.connect(_cleanup_dsn()) as c:
        c.execute("DELETE FROM trades WHERE run_id=%s", (run_id,))
        c.execute("DELETE FROM decisions WHERE run_id=%s", (run_id,))
        c.commit()
    _cleanup(sym)


def test_quarantined_trades_are_not_counted_as_closed_or_in_win_rate():
    """P0 REGRESSION: with 2 closed (1 win, 1 loss) + 1 quarantined + 1 open, the old
    `status <> 'open'` counted 3 closed and a 1/3 win rate. Correct is 2 closed and 1/2."""
    sym = "TST_" + os.urandom(3).hex().upper()
    run_id = "dash-" + os.urandom(3).hex()
    try:
        _seed_trade(sym, run_id, status="closed", r=1.5)      # win
        _seed_trade(sym, run_id, status="closed", r=-1.0)     # loss
        _seed_trade(sym, run_id, status="quarantined")        # NOT a result
        _seed_trade(sym, run_id, status="open")               # still running
        summary = _service().state(symbol=sym, run_id=run_id)["summary"]

        assert summary["trades_total"] == 4
        assert summary["trades_open"] == 1
        assert summary["trades_closed"] == 2, "quarantined must NOT count as closed"
        assert summary["trades_quarantined"] == 1, "quarantine must be reported separately"
        assert summary["wins"] == 1
        assert summary["win_rate"] == pytest.approx(0.5), "win-rate denominator must exclude quarantine"
    finally:
        _purge(sym, run_id)


def test_run_aggregation_separates_quarantined_from_closed():
    """P0: the same contamination existed in the per-run table feeding the Experimente tab."""
    sym = "TST_" + os.urandom(3).hex().upper()
    run_id = "dashrun-" + os.urandom(3).hex()
    try:
        _seed_trade(sym, run_id, status="closed", r=1.0)
        _seed_trade(sym, run_id, status="quarantined")
        runs = _service().state(symbol=sym, run_id=run_id)["runs"]
        row = next(r for r in runs if r["run_id"] == run_id)
        assert row["closed_trades"] == 1 and row["quarantined_trades"] == 1
    finally:
        _purge(sym, run_id)


def test_paid_spend_run_is_null_without_a_selected_run():
    """P1: without a run filter the card must NOT present the sum of ALL historical attempts as
    'this run's spend'. Day/month stay global; run is None."""
    state = _service().state(symbol="GOLD")
    assert state["paid_spend"].get("run") is None
    assert state["paid_spend"].get("day") is not None
    assert state["paid_spend"].get("month") is not None


def test_state_exposes_every_section_and_leaks_no_secret():
    """P0-2: the new queries had NO coverage. Exercise the whole real response shape once, and
    assert the redaction boundary holds end to end."""
    import json

    state = _service().state(symbol="GOLD", limit=5)
    for key in ("timeline", "open_trades", "quarantined_trades", "closed_trades", "runs",
                "datasets", "llm_calls", "paid_attempts", "downtime_gaps", "processed_bars",
                "snapshot_conflicts", "reservations", "services", "pipeline_runs", "db_tables"):
        assert isinstance(state[key], list), f"{key} missing/!list in the dashboard state"
    assert state["health"]["database"]["ok"] is True
    assert set(state["paid_spend"]) >= {"run", "day", "month", "unreconciled", "orphans"}
    # the DSN (with its password) is the most dangerous value in scope — it must never appear
    blob = json.dumps(state)
    assert DSN not in blob
    for marker in ("password=", "postgresql://"):
        assert marker not in blob


def test_utc_budget_windows_do_not_depend_on_session_timezone():
    """P1: the day/month labels say UTC, so the windows must be pinned to real UTC midnight rather
    than following the PostgreSQL session timezone."""
    with psycopg.connect(DSN) as c:
        c.execute("SET TIME ZONE 'Pacific/Kiritimati'")     # UTC+14: local 'today' != UTC 'today'
        session_day = c.execute("SELECT date_trunc('day', now())").fetchone()[0]
        utc_day = c.execute(
            "SELECT date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'").fetchone()[0]
    assert session_day != utc_day, "test precondition: the two windows must differ in this zone"
