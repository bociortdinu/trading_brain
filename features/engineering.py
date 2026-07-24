"""Deterministic feature engineering for one timeframe.

Turns a list of closed candles into a compact, JSON-safe feature dict. No LLM here;
pure math. The regime classifier combines EMA structure + ADX + slope (not a naive
MA cross) so trend and range are distinguished robustly.
"""

from __future__ import annotations

import numpy as np

from data_collector.providers.base import Candle
from features import indicators as ind

# Minimum bars needed for the slowest indicator (EMA-200).
MIN_BARS = 200
ADX_TREND = 25.0   # ADX at/above -> trending
ADX_RANGE = 20.0   # ADX below -> ranging


def _arrays(candles: list[Candle]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    close = np.array([c.close for c in candles], dtype=float)
    high = np.array([c.high for c in candles], dtype=float)
    low = np.array([c.low for c in candles], dtype=float)
    return high, low, close


def ema_alignment(close: np.ndarray) -> str:
    f, s, t = ind.ema(close, 21)[-1], ind.ema(close, 50)[-1], ind.ema(close, 200)[-1]
    if np.isnan([f, s, t]).any():
        return "mixed"
    if f > s > t:
        return "up"
    if f < s < t:
        return "down"
    return "mixed"


def classify_regime(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> str:
    align = ema_alignment(close)
    adx_arr, _, _ = ind.adx(high, low, close, 14)
    adx_last = adx_arr[-1]
    ema_slow = ind.ema(close, 50)
    slope = ind.linreg_slope(ema_slow[-20:]) / close[-1] if close[-1] else 0.0
    trending = not np.isnan(adx_last) and adx_last >= ADX_TREND

    if trending and align == "up" and slope > 0:
        return "bull_trend"
    if trending and align == "down" and slope < 0:
        return "bear_trend"
    if not np.isnan(adx_last) and adx_last < ADX_RANGE:
        return "range"
    return "choppy"


def nearest_resistance_pct(price: float, swing_highs: list[float]) -> float | None:
    """Distance to the nearest swing HIGH strictly ABOVE price (percent). None if none."""
    above = [h for h in swing_highs if h > price]
    if not above or not price:
        return None
    return round((min(above) - price) / price * 100, 3)


def nearest_support_pct(price: float, swing_lows: list[float]) -> float | None:
    """Distance to the nearest swing LOW strictly BELOW price (percent). None if none."""
    below = [lo for lo in swing_lows if lo < price]
    if not below or not price:
        return None
    return round((price - max(below)) / price * 100, 3)


def timeframe_features(candles: list[Candle]) -> dict:
    """Compact feature dict for one timeframe. Requires >= MIN_BARS closed candles."""
    if len(candles) < MIN_BARS:
        raise ValueError(f"need >= {MIN_BARS} candles, got {len(candles)}")
    high, low, close = _arrays(candles)
    price = float(close[-1])

    adx_arr, _, _ = ind.adx(high, low, close, 14)
    atr_arr = ind.atr(high, low, close, 14)
    rsi_arr = ind.rsi(close, 14)

    highs = [float(high[i]) for i in ind.swing_highs(high)]
    lows = [float(low[i]) for i in ind.swing_lows(low)]
    res_pct = nearest_resistance_pct(price, highs)   # resistance from swing HIGHS above only
    sup_pct = nearest_support_pct(price, lows)        # support from swing LOWS below only

    # Trend SLOPE of the EMA-50, normalized to % of price. Complements ADX: ADX gives trend
    # STRENGTH (magnitude), the slope gives DIRECTION + steepness. Same computation the
    # regime classifier uses, now surfaced for the decision input.
    ema_slow = ind.ema(close, 50)
    slope_pct = round(float(ind.linreg_slope(ema_slow[-20:])) / price * 100, 4) if price else None

    def r(x: float, n: int) -> float | None:
        return round(float(x), n) if not np.isnan(x) else None

    return {
        "regime": classify_regime(high, low, close),
        "ema_align": ema_alignment(close),
        "adx": r(adx_arr[-1], 1),
        "rsi": r(rsi_arr[-1], 1),
        "atr_pct": round(float(atr_arr[-1]) / price * 100, 3) if not np.isnan(atr_arr[-1]) and price else None,
        "ema50_slope_pct": slope_pct,
        "close": round(price, 4),
        "nearest_resistance_pct": res_pct,
        "nearest_support_pct": sup_pct,
        "bar_close": candles[-1].close_time.isoformat(),
    }


# `right` window used by ind.swing_highs/lows: a pivot at bar i is only CONFIRMED at bar i+right.
_SWING_RIGHT = 2


class TimeframeSeries:
    """Precompute a timeframe's indicator arrays ONCE, then read `timeframe_features` at ANY bar
    position in O(1) (+ the pivots so far). This turns a backtest's O(n^2) re-slicing (every bar
    recomputes every indicator over the whole prefix) into O(n).

    It is EXACTLY equivalent to `timeframe_features(candles[:p+1])`, never an approximation:
    - ema/atr/rsi/adx here are seeded from bar 0 and recursive, so `arr[p]` equals the last value
      of the same indicator over the prefix `candles[:p+1]` (position-independent);
    - a swing pivot at bar i uses bars up to i+right, so the pivots 'known' at bar p are exactly
      those with i <= p-right — which is precisely `swing_highs(high[:p+1])`.
    `test_engineering` asserts `features_at(p) == timeframe_features(candles[:p+1])` for every p.
    """

    def __init__(self, candles: list[Candle]) -> None:
        if len(candles) < MIN_BARS:
            raise ValueError(f"need >= {MIN_BARS} candles, got {len(candles)}")
        self.candles = candles
        self._high, self._low, self._close = _arrays(candles)
        self._ema21 = ind.ema(self._close, 21)
        self._ema50 = ind.ema(self._close, 50)
        self._ema200 = ind.ema(self._close, 200)
        self._adx = ind.adx(self._high, self._low, self._close, 14)[0]
        self._atr = ind.atr(self._high, self._low, self._close, 14)
        self._rsi = ind.rsi(self._close, 14)
        # Full pivot lists (sorted ascending); confirmed for a bar p when index <= p - right.
        self._swing_hi = ind.swing_highs(self._high)
        self._swing_lo = ind.swing_lows(self._low)

    def _align_at(self, p: int) -> str:
        f, s, t = self._ema21[p], self._ema50[p], self._ema200[p]
        if np.isnan([f, s, t]).any():
            return "mixed"
        if f > s > t:
            return "up"
        if f < s < t:
            return "down"
        return "mixed"

    def _slope_pct_at(self, p: int, price: float) -> float | None:
        slope = ind.linreg_slope(self._ema50[max(0, p - 19): p + 1])
        return round(float(slope) / price * 100, 4) if price else None

    def _regime_at(self, p: int, align: str, price: float) -> str:
        adx_last = self._adx[p]
        slope = ind.linreg_slope(self._ema50[max(0, p - 19): p + 1]) / price if price else 0.0
        trending = not np.isnan(adx_last) and adx_last >= ADX_TREND
        if trending and align == "up" and slope > 0:
            return "bull_trend"
        if trending and align == "down" and slope < 0:
            return "bear_trend"
        if not np.isnan(adx_last) and adx_last < ADX_RANGE:
            return "range"
        return "choppy"

    def features_at(self, p: int) -> dict:
        """`timeframe_features(candles[:p+1])`, by indexing the precomputed arrays."""
        if p < MIN_BARS - 1:
            raise ValueError(f"need >= {MIN_BARS} bars up to position {p}")
        price = float(self._close[p])
        cutoff = p - _SWING_RIGHT   # a pivot is confirmed only `right` bars later
        highs = [float(self._high[i]) for i in self._swing_hi if i <= cutoff]
        lows = [float(self._low[i]) for i in self._swing_lo if i <= cutoff]
        align = self._align_at(p)

        def r(x: float, n: int) -> float | None:
            return round(float(x), n) if not np.isnan(x) else None

        atr_p = self._atr[p]
        return {
            "regime": self._regime_at(p, align, price),
            "ema_align": align,
            "adx": r(self._adx[p], 1),
            "rsi": r(self._rsi[p], 1),
            "atr_pct": round(float(atr_p) / price * 100, 3) if not np.isnan(atr_p) and price else None,
            "ema50_slope_pct": self._slope_pct_at(p, price),
            "close": round(price, 4),
            "nearest_resistance_pct": nearest_resistance_pct(price, highs),
            "nearest_support_pct": nearest_support_pct(price, lows),
            "bar_close": self.candles[p].close_time.isoformat(),
        }
