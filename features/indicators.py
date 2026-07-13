"""Technical indicators — pure NumPy, deterministic, Wilder-consistent.

All functions take 1-D arrays (oldest-first) and return arrays of the same length
with NaN during the warm-up period. This is the mathematical core of the brain;
it is unit-tested against known values and properties.
"""

from __future__ import annotations

import numpy as np


def _rma(x: np.ndarray, period: int) -> np.ndarray:
    """Wilder's running moving average (RMA/SMMA), seeded with the SMA.

    Robust to leading NaNs: seeding starts at the first non-NaN value, so chained
    indicators (e.g. ADX's DX, which is NaN during DI warm-up) are not poisoned.
    """
    x = np.asarray(x, dtype=float)
    out = np.full(x.shape, np.nan)
    valid = np.flatnonzero(~np.isnan(x))
    if valid.size < period:
        return out
    start = int(valid[0])
    seed = start + period - 1
    if seed >= len(x):
        return out
    out[seed] = x[start : seed + 1].mean()
    for i in range(seed + 1, len(x)):
        xi = x[i]
        out[i] = out[i - 1] if np.isnan(xi) else (out[i - 1] * (period - 1) + xi) / period
    return out


def sma(values: np.ndarray, period: int) -> np.ndarray:
    values = np.asarray(values, dtype=float)
    out = np.full(values.shape, np.nan)
    if len(values) < period:
        return out
    cs = np.cumsum(np.insert(values, 0, 0.0))
    out[period - 1 :] = (cs[period:] - cs[:-period]) / period
    return out


def ema(values: np.ndarray, period: int) -> np.ndarray:
    values = np.asarray(values, dtype=float)
    out = np.full(values.shape, np.nan)
    if len(values) < period:
        return out
    k = 2.0 / (period + 1)
    out[period - 1] = values[:period].mean()  # seed with SMA
    for i in range(period, len(values)):
        out[i] = values[i] * k + out[i - 1] * (1 - k)
    return out


def true_range(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
    high = np.asarray(high, float)
    low = np.asarray(low, float)
    close = np.asarray(close, float)
    prev_close = np.roll(close, 1)
    tr = np.maximum(high - low, np.maximum(np.abs(high - prev_close), np.abs(low - prev_close)))
    tr[0] = high[0] - low[0]
    return tr


def atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> np.ndarray:
    return _rma(true_range(high, low, close), period)


def rsi(close: np.ndarray, period: int = 14) -> np.ndarray:
    close = np.asarray(close, float)
    out = np.full(close.shape, np.nan)
    if len(close) < period + 1:
        return out
    delta = np.diff(close)
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    avg_gain = _rma(gain, period)
    avg_loss = _rma(loss, period)
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = np.where(avg_loss != 0, avg_gain / avg_loss, np.inf)
        r = 100.0 - 100.0 / (1.0 + rs)
    out[1:] = r  # delta[i] corresponds to close[i+1]
    return out


def adx(
    high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return (adx, plus_di, minus_di), Wilder-smoothed."""
    high = np.asarray(high, float)
    low = np.asarray(low, float)
    close = np.asarray(close, float)

    up = high - np.roll(high, 1)
    down = np.roll(low, 1) - low
    up[0] = np.nan
    down[0] = np.nan
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)

    atr_ = _rma(true_range(high, low, close), period)
    with np.errstate(divide="ignore", invalid="ignore"):
        plus_di = 100.0 * _rma(plus_dm, period) / atr_
        minus_di = 100.0 * _rma(minus_dm, period) / atr_
        denom = plus_di + minus_di
        dx = np.where(denom != 0, 100.0 * np.abs(plus_di - minus_di) / denom, np.nan)
    adx_ = _rma(dx, period)
    return adx_, plus_di, minus_di


def linreg_slope(values: np.ndarray) -> float:
    """Least-squares slope of `values` vs index (NaNs dropped). 0 if < 2 points."""
    v = np.asarray(values, float)
    v = v[~np.isnan(v)]
    if len(v) < 2:
        return 0.0
    x = np.arange(len(v), dtype=float)
    return float(np.polyfit(x, v, 1)[0])


def swing_highs(high: np.ndarray, left: int = 2, right: int = 2) -> list[int]:
    """Indices of strict fractal swing highs (unique max within [i-left, i+right])."""
    high = np.asarray(high, float)
    idx: list[int] = []
    for i in range(left, len(high) - right):
        window = high[i - left : i + right + 1]
        if high[i] == window.max() and np.count_nonzero(window == high[i]) == 1:
            idx.append(i)
    return idx


def swing_lows(low: np.ndarray, left: int = 2, right: int = 2) -> list[int]:
    low = np.asarray(low, float)
    idx: list[int] = []
    for i in range(left, len(low) - right):
        window = low[i - left : i + right + 1]
        if low[i] == window.min() and np.count_nonzero(window == low[i]) == 1:
            idx.append(i)
    return idx
