"""Indicator correctness: exact small cases + Wilder properties."""

from __future__ import annotations

import numpy as np
import pytest

from features import indicators as ind


def test_ema_of_constant_is_constant():
    e = ind.ema(np.full(50, 5.0), 10)
    assert e[-1] == 5.0
    assert np.isnan(e[:9]).all()  # warm-up


def test_sma_basic():
    s = ind.sma(np.arange(1.0, 11.0), 5)  # 1..10
    assert s[4] == 3.0  # mean(1..5)
    assert s[-1] == 8.0  # mean(6..10)


def test_rsi_all_gains_is_100():
    r = ind.rsi(np.arange(1.0, 40.0), 14)  # strictly increasing
    assert round(r[-1], 6) == 100.0


def test_rsi_all_losses_is_0():
    r = ind.rsi(np.arange(40.0, 1.0, -1.0), 14)  # strictly decreasing
    assert round(r[-1], 6) == 0.0


def test_atr_constant_range_equals_range():
    n = 50
    close = np.full(n, 100.0)
    high = close + 1.0
    low = close - 1.0
    a = ind.atr(high, low, close, 14)
    assert round(a[-1], 6) == 2.0  # TR is a constant 2.0


def test_true_range_uses_prev_close():
    high = np.array([10.0, 12.0])
    low = np.array([9.0, 11.0])
    close = np.array([9.5, 11.5])
    tr = ind.true_range(high, low, close)
    assert tr[0] == 1.0  # first bar: high-low
    assert tr[1] == max(12 - 11, abs(12 - 9.5), abs(11 - 9.5))  # = 2.5


def test_adx_uptrend_plus_di_dominates_and_bounded():
    close = np.arange(2000.0, 2250.0, 1.0)  # 250 bars, steady up
    high, low = close + 0.3, close - 0.3
    adx, plus_di, minus_di = ind.adx(high, low, close, 14)
    assert plus_di[-1] > minus_di[-1]
    assert 0.0 <= adx[-1] <= 100.0
    assert adx[-1] >= 25.0  # a clean trend is "trending"


def test_linreg_slope_positive_for_increasing():
    assert ind.linreg_slope(np.array([1.0, 2.0, 3.0, 4.0])) == pytest.approx(1.0)


def test_swing_highs_and_lows():
    # 7 bars; with left=right=2 the testable index range is [2, 4].
    h = np.array([1.0, 2.0, 5.0, 2.0, 1.0, 2.0, 1.0])   # unique max at index 2
    lo = np.array([5.0, 4.0, 1.0, 4.0, 5.0, 4.0, 5.0])  # unique min at index 2
    assert 2 in ind.swing_highs(h, 2, 2)
    assert 2 in ind.swing_lows(lo, 2, 2)
