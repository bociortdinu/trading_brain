"""Regime, S/R separation, MTF packet + as_of anchoring, confluence (D1), anti look-ahead."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from data_collector.providers.base import only_closed
from features.engineering import classify_regime, nearest_resistance_pct, nearest_support_pct, timeframe_features
from features.mtf import build_feature_packet, confluence
from tests.synthetic import oscillating, trend

_END = datetime(2026, 7, 1, tzinfo=timezone.utc)


def _hlc(candles):
    return (
        np.array([c.high for c in candles]),
        np.array([c.low for c in candles]),
        np.array([c.close for c in candles]),
    )


def _mtf(end=_END):
    """All timeframes anchored so every last close_time == end."""
    def gen(tf_min):
        return trend(step=1.0, tf_min=tf_min, start=end - timedelta(minutes=tf_min * 250))
    return {"1day": gen(1440), "4h": gen(240), "1h": gen(60), "15min": gen(15)}


# ---- regime ---- #
def test_regime_bull_trend():
    assert classify_regime(*_hlc(trend(step=1.0))) == "bull_trend"


def test_regime_bear_trend():
    assert classify_regime(*_hlc(trend(step=-1.0, start_price=2500.0))) == "bear_trend"


def test_regime_non_trending():
    assert classify_regime(*_hlc(oscillating())) in {"range", "choppy"}


# ---- S/R separation (would FAIL with the old pooled implementation) ---- #
def test_resistance_and_support_are_separated():
    price = 100.0
    swing_highs = [98.0, 105.0]  # 98 below price, 105 above
    swing_lows = [95.0, 102.0]   # 95 below price, 102 above
    # resistance must be the nearest HIGH above (105 -> 5%), NOT the low-above 102 (2%)
    assert nearest_resistance_pct(price, swing_highs) == pytest.approx(5.0)
    # support must be the nearest LOW below (95 -> 5%), NOT the high-below 98 (2%)
    assert nearest_support_pct(price, swing_lows) == pytest.approx(5.0)


def test_resistance_none_when_no_high_above():
    # A swing LOW above price must not be reported as resistance.
    assert nearest_resistance_pct(100.0, [90.0, 95.0]) is None


def test_support_none_when_no_low_below():
    assert nearest_support_pct(100.0, [110.0, 120.0]) is None


# ---- features + confluence ---- #
def test_timeframe_features_shape():
    f = timeframe_features(trend(step=1.0))
    assert f["regime"] == "bull_trend"
    assert set(f) >= {"regime", "ema_align", "adx", "rsi", "atr_pct", "close", "bar_close"}


def test_timeframe_features_requires_enough_bars():
    with pytest.raises(ValueError):
        timeframe_features(trend(n=50))


def test_confluence_uses_d1_macro():
    up = {"ema_align": "up", "regime": "bull_trend"}
    down = {"ema_align": "down", "regime": "bear_trend"}
    mixed_macro = {"ema_align": "mixed", "regime": "bull_trend"}
    rng = {"regime": "range"}
    assert confluence(up, up, up, up) == "aligned_bull"
    assert confluence(down, down, down, down) == "aligned_bear"
    # D1 macro disagrees (mixed) -> not "aligned", falls through to pullback/mixed
    assert confluence(mixed_macro, up, up, rng) == "bull_pullback"
    assert confluence(mixed_macro, up, up, up) == "mixed"


# ---- MTF packet + as_of anchoring ---- #
def test_build_feature_packet_anchored():
    tf = _mtf()
    packet = build_feature_packet(
        "GOLD", tf, as_of=_END, provider="csv", provider_symbol="GOLD",
        ingested_at=datetime.now(timezone.utc), spread_pct=0.02,
    )
    assert packet.bar_close == _END
    assert packet.regime == "bull_trend"       # H1
    assert packet.major_trend == "bull_trend"  # H4
    assert packet.macro_bias == "up"           # D1
    assert packet.confluence == "aligned_bull"
    assert packet.provider == "csv" and packet.provider_symbol == "GOLD"
    assert packet.interval_list == ["1day", "4h", "1h", "15min"]
    # per-timeframe data-quality is computed for audit
    assert set(packet.data_quality) == {"1day", "4h", "1h", "15min"}
    assert packet.data_quality["15min"]["verdict"] == "ok"


def test_build_feature_packet_rejects_bar_after_as_of():
    tf = _mtf()
    with pytest.raises(ValueError):
        build_feature_packet(
            "GOLD", tf, as_of=_END - timedelta(minutes=15), provider="csv",
            provider_symbol="GOLD", ingested_at=datetime.now(timezone.utc),
        )


def test_build_feature_packet_missing_timeframe():
    with pytest.raises(ValueError):
        build_feature_packet(
            "GOLD", {"15min": trend()}, as_of=_END, provider="csv",
            provider_symbol="GOLD", ingested_at=datetime.now(timezone.utc),
        )


def test_only_closed_drops_forming_bar():
    candles = trend(n=5)
    now = candles[-1].close_time - timedelta(seconds=1)
    closed = only_closed(candles, now)
    assert len(closed) == 4
    assert all(c.close_time <= now for c in closed)
