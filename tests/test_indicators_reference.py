"""Cross-validate EMA/RSI/ATR/ADX against an INDEPENDENT implementation (pandas ewm).

Property tests aren't enough to claim "Wilder-consistent". Here we compute the same
indicators on the same bars via pandas' Exponentially-Weighted engine (alpha=1/period,
Wilder smoothing) and compare the converged tail. Seeds differ (SMA vs first-value) but
decay geometrically, so late values must agree tightly.
"""

from __future__ import annotations

import numpy as np
import pytest

from features import indicators as ind

pd = pytest.importorskip("pandas")

N = 14


def _series(seed: int = 42, n: int = 400):
    rng = np.random.default_rng(seed)
    steps = rng.normal(0, 1.0, n)
    close = 2000.0 + np.cumsum(steps)
    noise = rng.uniform(0.2, 1.5, n)
    high = close + noise
    low = close - noise
    return high, low, close


def _rma_ref(x):
    return pd.Series(x).ewm(alpha=1.0 / N, adjust=False).mean()


def test_ema_matches_reference():
    _, _, close = _series()
    ref = pd.Series(close).ewm(span=N, adjust=False).mean().to_numpy()
    got = ind.ema(close, N)
    assert got[-1] == pytest.approx(ref[-1], rel=1e-4)


def test_rsi_matches_reference():
    _, _, close = _series()
    s = pd.Series(close)
    delta = s.diff()
    ag = _rma_ref(delta.clip(lower=0).fillna(0))
    al = _rma_ref((-delta.clip(upper=0)).fillna(0))
    rs = ag / al
    ref = (100 - 100 / (1 + rs)).to_numpy()
    got = ind.rsi(close, N)
    assert got[-1] == pytest.approx(ref[-1], abs=0.05)


def test_atr_matches_reference():
    high, low, close = _series()
    h, l, c = pd.Series(high), pd.Series(low), pd.Series(close)
    prev = c.shift(1)
    tr = pd.concat([h - l, (h - prev).abs(), (l - prev).abs()], axis=1).max(axis=1)
    tr.iloc[0] = high[0] - low[0]
    ref = _rma_ref(tr).to_numpy()
    got = ind.atr(high, low, close, N)
    assert got[-1] == pytest.approx(ref[-1], rel=1e-4)


def test_adx_matches_reference():
    high, low, close = _series()
    h, l, c = pd.Series(high), pd.Series(low), pd.Series(close)
    up = h.diff()
    down = -l.diff()
    plus_dm = (((up > down) & (up > 0)) * up).fillna(0.0)
    minus_dm = (((down > up) & (down > 0)) * down).fillna(0.0)
    prev = c.shift(1)
    tr = pd.concat([h - l, (h - prev).abs(), (l - prev).abs()], axis=1).max(axis=1)
    tr.iloc[0] = high[0] - low[0]
    atr = _rma_ref(tr)
    pdi = 100 * _rma_ref(plus_dm) / atr
    mdi = 100 * _rma_ref(minus_dm) / atr
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi)
    ref = _rma_ref(dx).to_numpy()
    got, _, _ = ind.adx(high, low, close, N)
    assert got[-1] == pytest.approx(ref[-1], abs=0.1)
