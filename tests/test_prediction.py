"""Strict validation of the /purchase payload + internal/external enum mapping."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from brokers_bridge.trading_hands import ModelSetup, PredictionRequest
from core.models import Direction, to_external_model_type


def _pred(**overrides):
    base = dict(
        symbol="GOLD",
        prediction_date="2026-07-12",
        allocation=1,
        preds_proba=0.6,
        stop_loss=1.0,
        take_profit=1.0,
        model_setup=ModelSetup(forecast_horizon=1, target_change=1, model_type="Buy"),
        runtimestamp="2026-07-12T10:00:00Z",
        sl_tp_logic="percent",
        interval="15m",
        id_model_properties="test",
    )
    base.update(overrides)
    return PredictionRequest(**base)


def test_valid_buy():
    assert _pred().model_setup.model_type == "Buy"


def test_proba_below_half_rejected():
    with pytest.raises(ValidationError):
        _pred(preds_proba=0.49)


def test_negative_sl_rejected():
    with pytest.raises(ValidationError):
        _pred(stop_loss=-1.0)


def test_zero_sl_rejected():
    with pytest.raises(ValidationError):
        _pred(stop_loss=0.0)


def test_invalid_model_type_rejected():
    with pytest.raises(ValidationError):
        ModelSetup(forecast_horizon=1, target_change=1, model_type="buy")  # wrong case


def test_buy_requires_nonnegative_tp():
    with pytest.raises(ValidationError):
        _pred(
            take_profit=-1.0,
            model_setup=ModelSetup(forecast_horizon=1, target_change=1, model_type="Buy"),
        )


def test_sell_requires_negative_tp():
    with pytest.raises(ValidationError):
        _pred(
            take_profit=1.0,
            model_setup=ModelSetup(forecast_horizon=1, target_change=1, model_type="Sell"),
        )


def test_sell_with_negative_tp_ok():
    p = _pred(
        take_profit=-1.5,
        model_setup=ModelSetup(forecast_horizon=1, target_change=1, model_type="Sell"),
    )
    assert p.take_profit == -1.5


def test_tp_magnitude_bounded():
    with pytest.raises(ValidationError):
        _pred(take_profit=250.0)


def test_internal_external_enum_mapping():
    assert to_external_model_type(Direction.BUY) == "Buy"
    assert to_external_model_type(Direction.SELL) == "Sell"
    assert to_external_model_type(Direction.NO_TRADE) == "NoAction"
