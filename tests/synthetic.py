"""Deterministic synthetic OHLCV for tests (no network, no real market data)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from data_collector.providers.base import Candle

_T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _candle(base: datetime, i: int, o: float, c: float, spread: float, tf_min: int) -> Candle:
    ot = base + timedelta(minutes=tf_min * i)
    return Candle(
        open_time=ot,
        close_time=ot + timedelta(minutes=tf_min),
        open=o,
        high=max(o, c) + spread,
        low=min(o, c) - spread,
        close=c,
        volume=1.0,
    )


def trend(
    n: int = 250, start_price: float = 2000.0, step: float = 1.0, spread: float = 0.3,
    tf_min: int = 15, start: datetime | None = None,
) -> list[Candle]:
    """Clean directional trend (step>0 up, step<0 down). `start` = first bar's open time."""
    base = start or _T0
    out, price = [], start_price
    for i in range(n):
        out.append(_candle(base, i, price, price + step, spread, tf_min))
        price += step
    return out


def oscillating(
    n: int = 250, mean: float = 2000.0, amp: float = 2.0, spread: float = 0.5,
    tf_min: int = 15, start: datetime | None = None,
) -> list[Candle]:
    """Flat range: close alternates around a mean -> low/undefined ADX."""
    base = start or _T0
    out = []
    for i in range(n):
        o = mean + (amp if i % 2 else -amp)
        c = mean + (-amp if i % 2 else amp)
        out.append(_candle(base, i, o, c, spread, tf_min))
    return out
