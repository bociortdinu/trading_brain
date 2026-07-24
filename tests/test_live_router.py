"""Live execution router: every gate, and the payload contract.

This is the only code in trading_brain that sends a real order, so the tests are written the
pessimistic way round — each one asserts that a *refusal* happened and that `purchase` was never
called, rather than only checking the happy path.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from brokers_bridge.trading_hands import Position, Status, TradeResult
from core.models import Direction
from execution.live_router import (
    LiveExecutionConfig,
    LiveRouter,
    build_prediction,
    live_config_from_settings,
)
from tests.helpers import run

NOW = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)


class _Client:
    """Records what the router asked for, so a test can assert no order was attempted."""

    def __init__(self, *, trading_enabled=True, environment="demo", positions=None,
                 trading_volume=0.02,
                 result=TradeResult(accepted=True, external_id="X1", symbol="GOLD",
                                    side="sell", volume=0.01)):
        self._status = Status(connected=True, account="1", environment=environment,
                              trading_enabled=trading_enabled, trading_volume=trading_volume)
        self._positions = positions or []
        self._result = result
        self.purchases: list = []

    async def status(self):
        return self._status

    async def positions(self):
        return list(self._positions)

    async def purchase(self, prediction):
        self.purchases.append(prediction)
        return self._result


def _router(client, **cfg):
    base = dict(enabled=True, volume=0.01, max_volume=0.10, cooldown_minutes=15.0,
                max_open_positions=1)
    base.update(cfg)
    return LiveRouter(client, LiveExecutionConfig(**base), now_fn=lambda: NOW)


def _route(router, **kw):
    base = dict(symbol="GOLD", direction=Direction.SELL, approved=True, sl_pct=0.3, tp_pct=0.6,
                confidence=0.6, as_of=NOW)
    base.update(kw)
    return run(router.route(**base))


# ---- the gates ---- #
def test_disabled_router_never_orders():
    c = _Client()
    r = _route(_router(c, enabled=False))
    assert r.placed is False and r.reason == "router_disabled" and c.purchases == []


def test_unapproved_verdict_never_orders():
    c = _Client()
    r = _route(_router(c), approved=False)
    assert r.placed is False and r.reason == "not_approved" and c.purchases == []


def test_no_trade_direction_never_orders():
    c = _Client()
    r = _route(_router(c), direction=Direction.NO_TRADE)
    assert r.placed is False and c.purchases == []


def test_missing_sl_or_tp_never_orders():
    c = _Client()
    assert _route(_router(c), sl_pct=None).reason == "missing_sl_tp"
    assert _route(_router(c), tp_pct=None).reason == "missing_sl_tp"
    assert c.purchases == []


def test_volume_above_the_cap_never_orders():
    c = _Client()
    r = _route(_router(c, volume=0.5, max_volume=0.1))
    assert r.reason == "volume_out_of_bounds" and c.purchases == []


def test_trading_disabled_at_the_broker_is_not_overridden():
    """trading_hands owns the final say; the brain's own flag cannot override its refusal."""
    c = _Client(trading_enabled=False)
    r = _route(_router(c))
    assert r.reason == "trading_disabled" and c.purchases == []


def test_unknown_trading_flag_is_treated_as_refusal():
    """An older binary reports None. An unreported safety flag must never read as permission."""
    c = _Client(trading_enabled=None)
    r = _route(_router(c))
    assert r.reason == "trading_disabled" and c.purchases == []


def test_non_demo_account_is_refused_by_default():
    c = _Client(environment="real")
    r = _route(_router(c))
    assert r.reason.startswith("not_demo") and c.purchases == []


def test_existing_position_on_the_symbol_blocks_a_duplicate():
    """The gate that matters most: broker truth, not shadow state. After a restart the shadow
    side has forgotten the position while the account still holds it."""
    c = _Client(positions=[Position(symbol="GOLD", instrument_id=1, volume=0.01,
                                    open_price=4000.0, side="sell", external_id="OPEN-1")])
    r = _route(_router(c))
    assert r.reason == "position_exists:OPEN-1" and c.purchases == []


def test_position_on_a_different_symbol_still_counts_against_max_positions():
    c = _Client(positions=[Position(symbol="SILVER", instrument_id=2, volume=0.01,
                                    open_price=30.0, side="buy", external_id="OPEN-2")])
    r = _route(_router(c, max_open_positions=1))
    assert r.reason.startswith("max_positions") and c.purchases == []


def test_cooldown_blocks_a_rapid_second_order():
    c = _Client()
    router = _router(c, cooldown_minutes=15.0)
    assert _route(router).placed is True
    second = _route(router)                      # same NOW -> zero minutes elapsed
    assert second.reason.startswith("cooldown") and len(c.purchases) == 1


def test_cooldown_expires():
    c = _Client()
    router = _router(c, cooldown_minutes=15.0)
    router.last_fired_at = NOW - timedelta(minutes=20)
    assert _route(router).placed is True


def test_a_refused_order_does_not_start_the_cooldown():
    """A broker rejection must not lock the router out of the next genuine signal."""
    c = _Client(result=TradeResult(accepted=False))
    router = _router(c)
    assert _route(router).reason == "broker_rejected"
    assert router.last_fired_at is None


def test_broker_no_action_is_reported_not_swallowed():
    c = _Client(result=None)
    r = _route(_router(c))
    assert r.placed is False and r.reason == "broker_no_action"


# ---- the happy path ---- #
def test_approved_verdict_places_the_order():
    c = _Client()
    r = _route(_router(c))
    assert r.placed is True and r.external_id == "X1" and len(c.purchases) == 1


def test_gates_checked_are_reported_for_audit():
    c = _Client()
    r = _route(_router(c))
    assert "existing_positions" in r.gates_checked and "trading_enabled" in r.gates_checked


# ---- payload contract ---- #
def test_sell_encodes_direction_as_a_negative_take_profit():
    p = build_prediction(symbol="GOLD", direction=Direction.SELL, sl_pct=0.3, tp_pct=0.6,
                         confidence=0.6, volume=0.01, as_of=NOW)
    assert p.model_setup.model_type == "Sell" and p.take_profit < 0 and p.stop_loss > 0


def test_buy_encodes_direction_as_a_positive_take_profit():
    p = build_prediction(symbol="GOLD", direction=Direction.BUY, sl_pct=0.3, tp_pct=0.6,
                         confidence=0.6, volume=0.01, as_of=NOW)
    assert p.model_setup.model_type == "Buy" and p.take_profit > 0


def test_confidence_below_the_broker_minimum_is_clamped_not_rescaled():
    """The broker rejects preds_proba < 0.5. Our confidence is ORDINAL, so it is clamped —
    rescaling it would assert a probability the model never claimed."""
    p = build_prediction(symbol="GOLD", direction=Direction.SELL, sl_pct=0.3, tp_pct=0.6,
                         confidence=0.1, volume=0.01, as_of=NOW)
    assert p.preds_proba == 0.5


def test_broker_volume_above_our_cap_is_refused():
    """trading_hands sizes every order from its OWN config and ignores our allocation, so the
    only real cap is refusing to trade when its size exceeds what we accept."""
    c = _Client(trading_volume=1.0)
    r = _route(_router(c, max_volume=0.10))
    assert r.reason.startswith("broker_volume_too_large") and c.purchases == []


def test_unknown_broker_volume_is_refused():
    """An older binary does not publish it. An unknown position size is not a safe one."""
    c = _Client(trading_volume=None)
    r = _route(_router(c))
    assert r.reason == "broker_volume_unknown" and c.purchases == []


def test_reported_volume_is_the_brokers_not_ours():
    c = _Client(trading_volume=0.02,
                result=TradeResult(accepted=True, external_id="X1", symbol="GOLD",
                                   side="sell", volume=0.02))
    r = _route(_router(c, volume=0.01))
    assert r.placed is True and r.volume == 0.02


def test_settings_default_to_a_disabled_router():
    from config.settings import Settings

    cfg = live_config_from_settings(Settings(db_dsn="postgresql://u:p@127.0.0.1:5432/x"))
    assert cfg.enabled is False and cfg.require_demo is True
