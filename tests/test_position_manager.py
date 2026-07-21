"""Live position manager: the exit half of the lifecycle.

The router opens; without this nothing closes. These tests pin the three outcomes a tick can
resolve, and — more importantly — pin what is NOT invented when the broker closes a position
behind our back.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from brokers_bridge.trading_hands import Balance, Position, Quote, TradeResult
from execution.position_manager import (
    LivePositionManager,
    horizon_for,
    r_from_prices,
)
from tests.helpers import run

NOW = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)


def _trade(**kw):
    base = dict(id=1, decision_id=10, run_id="r1", symbol="GOLD", side="sell",
                external_id="X1", entry_price=4000.0, sl_price=4012.0, tp_price=3976.0,
                opened_at=NOW - timedelta(hours=1), timeout_bars=96, timeframe="15min",
                costs={})
    base.update(kw)
    return base


class _Client:
    def __init__(self, positions=(), close_accepted=True, bid=3990.0, ask=3990.7):
        self._positions = list(positions)
        self._close_accepted = close_accepted
        self._quote = Quote(symbol="GOLD", bid=bid, ask=ask, time=0)
        self.closed: list[str] = []

    async def positions(self):
        return list(self._positions)

    async def balance(self):
        return Balance(balance=50000.0, equity=50000.0, free_margin=50000.0,
                       currency="RON", account="1")

    async def quote(self, symbol):
        return self._quote

    async def close(self, external_id):
        self.closed.append(external_id)
        return TradeResult(accepted=self._close_accepted, external_id=external_id)


def _pos(external_id="X1", symbol="GOLD"):
    return Position(symbol=symbol, instrument_id=1, volume=0.02, open_price=4000.0,
                    side="sell", external_id=external_id)


def _manager(monkeypatch, client, open_trades, recorded):
    import execution.position_manager as pm

    monkeypatch.setattr("database.repository.open_live_trades",
                        lambda dsn, symbol=None: list(open_trades))

    def _close(dsn, **kw):
        recorded.append(kw)
        return "closed"

    monkeypatch.setattr("database.repository.close_live_trade", _close)
    return pm.LivePositionManager(client, "postgresql://x", now_fn=lambda: NOW)


# ---- horizon ---- #
def test_horizon_comes_from_the_values_frozen_on_the_trade():
    """Read off the row, not the live config: changing the config must not move the deadline of
    a position that is already running."""
    assert horizon_for(_trade(timeout_bars=96, timeframe="15min")) == timedelta(hours=24)
    assert horizon_for(_trade(timeout_bars=4, timeframe="1h")) == timedelta(hours=4)


# ---- R from prices ---- #
def test_r_is_computed_from_the_entry_to_stop_distance():
    t = _trade(side="sell", entry_price=4000.0, sl_price=4012.0)   # 1R = 12.0
    assert r_from_prices(t, 3988.0) == 1.0        # a full R in favour
    assert r_from_prices(t, 4012.0) == -1.0       # stopped out
    assert r_from_prices(_trade(side="buy", entry_price=4000.0, sl_price=3988.0),
                         4012.0) == 1.0


def test_r_is_none_when_there_is_no_risk_distance():
    assert r_from_prices(_trade(entry_price=4000.0, sl_price=4000.0), 4010.0) is None


# ---- still held ---- #
def test_a_position_inside_its_horizon_is_left_alone(monkeypatch):
    client = _Client(positions=[_pos()])
    recorded: list = []
    report = run(_manager(monkeypatch, client, [_trade()], recorded).sweep())
    assert [e.action for e in report.exits] == ["held"]
    assert client.closed == [] and recorded == []


# ---- past the horizon ---- #
def test_a_position_past_its_horizon_is_closed_and_priced(monkeypatch):
    """We caused this exit, so the fill IS known and R is computable."""
    client = _Client(positions=[_pos()], bid=3990.0, ask=3990.7)
    recorded: list = []
    old = _trade(opened_at=NOW - timedelta(hours=30))
    report = run(_manager(monkeypatch, client, [old], recorded).sweep())

    assert client.closed == ["X1"]
    exit_ = report.exits[0]
    assert exit_.action == "closed_timeout" and exit_.exit_reason == "timeout"
    assert recorded[0]["exit_price"] == 3990.7          # a sell exits at the ask
    assert recorded[0]["exit_reason"] == "timeout"
    assert recorded[0]["r_multiple"] == pytest.approx(0.775, abs=1e-3)


def test_a_rejected_close_is_reported_and_retried_not_recorded(monkeypatch):
    """A failed close must not mark the trade closed — the position is still out there."""
    client = _Client(positions=[_pos()], close_accepted=False)
    recorded: list = []
    old = _trade(opened_at=NOW - timedelta(hours=30))
    report = run(_manager(monkeypatch, client, [old], recorded).sweep())
    assert report.exits[0].action == "close_failed" and recorded == []


# ---- broker closed it ---- #
def test_a_broker_closed_position_is_recorded_without_an_invented_price(monkeypatch):
    """The CoreAPI publishes neither the fill nor which level fired. Recording the exit AT the
    SL or TP would look like a measurement and frequently be wrong (slippage, gaps), and every R
    derived from it would inherit that."""
    client = _Client(positions=[])                    # gone from the account
    recorded: list = []
    report = run(_manager(monkeypatch, client, [_trade()], recorded).sweep())

    exit_ = report.exits[0]
    assert exit_.action == "closed_by_broker"
    assert exit_.r_multiple is None and exit_.realized_pnl is None
    assert recorded[0]["exit_price"] is None and recorded[0]["r_multiple"] is None
    assert "not inventing" in (exit_.detail or "") or "without" in (exit_.detail or "")
    assert client.closed == []                        # nothing to close, it is already gone


# ---- orphans ---- #
def test_a_broker_position_we_have_no_record_of_is_reported_not_closed(monkeypatch):
    """We do not know its horizon or intent, so it is surfaced rather than acted on."""
    client = _Client(positions=[_pos("UNKNOWN-9")])
    recorded: list = []
    report = run(_manager(monkeypatch, client, [], recorded).sweep())
    assert report.orphans == ["UNKNOWN-9"] and client.closed == []


def test_a_known_position_is_not_an_orphan(monkeypatch):
    client = _Client(positions=[_pos("X1")])
    report = run(_manager(monkeypatch, client, [_trade()], []).sweep())
    assert report.orphans == []


def test_multiple_positions_are_each_resolved(monkeypatch):
    client = _Client(positions=[_pos("X1")])          # X2 is gone, X1 still held
    recorded: list = []
    trades = [_trade(id=1, external_id="X1"), _trade(id=2, external_id="X2")]
    report = run(_manager(monkeypatch, client, trades, recorded).sweep())
    actions = {e.external_id: e.action for e in report.exits}
    assert actions == {"X1": "held", "X2": "closed_by_broker"}
    assert report.checked == 2
