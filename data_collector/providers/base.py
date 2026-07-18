"""Market-data provider interface + strict candle/series validation.

MVP: a `Protocol` (composition + typed config), not a plugin registry. A new data
source is a new class implementing `MarketDataProvider`, selected via config.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, field_validator, model_validator

# Canonical timeframe -> duration in minutes. The keys match config.timeframes.
TIMEFRAME_MINUTES: dict[str, int] = {
    "1min": 1,        # not a decision timeframe; used only for finer intrabar reconciliation
    "15min": 15,
    "1h": 60,
    "4h": 240,
    "1day": 1440,
}


def timeframe_minutes(timeframe: str) -> int:
    try:
        return TIMEFRAME_MINUTES[timeframe]
    except KeyError as exc:
        raise ValueError(f"unknown timeframe {timeframe!r}; known: {list(TIMEFRAME_MINUTES)}") from exc


# --------------------------------------------------------------------------- #
# Canonical bucket grid (the temporal contract).
#
# Every timeframe's bars are anchored to the Unix epoch in UTC:
#   15min -> :00/:15/:30/:45   1h -> :00   4h -> 00/04/08/12/16/20 UTC   1day -> 00:00 UTC
# This grid is DST-INVARIANT by construction: it never shifts when US clocks change,
# so buckets stay deterministic and scheduler-compatible across a DST transition.
# Session/DST semantics (when the market is open, the daily maintenance break, the
# weekly open/close in America/New_York) live in the CALENDAR (data_collector.session),
# NOT in the bar grid — the two concerns are deliberately separated.
#
# Determinism: the request `from` is floored to this grid (see the polygon provider),
# so the SAME logical period requested at different wall-clock moments within the same
# bar produces the SAME buckets. Returned bars are then VERIFIED on-grid (validate_series
# raises), so an off-grid / phase-shifted series can never be accepted silently.
# --------------------------------------------------------------------------- #
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

# Timeframes that MUST sit on the canonical UTC-epoch grid. The scheduler wakes on M15
# closes and the pipeline slices on hourly boundaries, so these are UTC-aligned regardless
# of source (any whole-hour broker offset still lands 15min->:00/:15/:30/:45 and 1h->:00).
# 4h/1day bars are anchored by the SOURCE's trading day instead: e.g. XTB opens the daily
# bar at broker midnight (22:00 UTC in summer) and its 4h buckets at 02/06/10/14/18/22 UTC,
# offset from UTC and shifting with broker DST. For those we require only consistent spacing
# + monotonicity (determinism from the source's fixed anchor), not a UTC-epoch phase.
_EPOCH_ALIGNED_TFS = frozenset({"1min", "15min", "1h"})


def floor_to_grid(dt: datetime, timeframe: str) -> datetime:
    """Floor `dt` down to the canonical UTC-epoch bucket boundary for `timeframe`."""
    dt = dt.astimezone(timezone.utc)
    step = timedelta(minutes=timeframe_minutes(timeframe))
    n = (dt - _EPOCH) // step
    return _EPOCH + n * step


def is_grid_aligned(dt: datetime, timeframe: str) -> bool:
    """True iff `dt` sits exactly on the canonical UTC-epoch grid for `timeframe`."""
    dt = dt.astimezone(timezone.utc)
    step = timedelta(minutes=timeframe_minutes(timeframe))
    return (dt - _EPOCH) % step == timedelta(0)


class Candle(BaseModel):
    open_time: datetime   # bar open (inclusive)
    close_time: datetime  # bar close (exclusive boundary == next bar's open)
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    @field_validator("open_time", "close_time")
    @classmethod
    def _tz_aware_utc(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware")
        return v.astimezone(timezone.utc)

    @field_validator("open", "high", "low", "close")
    @classmethod
    def _finite_positive(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("OHLC values must be finite")
        if v <= 0:
            raise ValueError("OHLC values must be positive")
        return v

    @field_validator("volume")
    @classmethod
    def _finite_nonneg(cls, v: float) -> float:
        if not math.isfinite(v) or v < 0:
            raise ValueError("volume must be finite and non-negative")
        return v

    @model_validator(mode="after")
    def _coherent(self) -> "Candle":
        if self.close_time <= self.open_time:
            raise ValueError("close_time must be after open_time")
        if self.high < self.low:
            raise ValueError("high < low")
        if self.high < max(self.open, self.close) or self.low > min(self.open, self.close):
            raise ValueError("high/low do not bound open/close")
        return self


class SeriesGap(BaseModel):
    """A missing run of bars between two present bars (NOT filled)."""
    after: datetime      # close_time of the bar before the gap
    before: datetime     # open_time of the bar after the gap
    missing_bars: int


@runtime_checkable
class MarketDataProvider(Protocol):
    async def get_ohlcv(self, symbol: str, timeframe: str, count: int) -> list[Candle]:
        """Return the last `count` CLOSED candles for (symbol, timeframe),
        oldest first. Implementations MUST NOT return a still-forming bar and
        MUST NOT fabricate/forward-fill missing bars."""
        ...


def only_closed(candles: list[Candle], now: datetime | None = None) -> list[Candle]:
    """Drop any bar that has not closed yet (close_time > now). Anti look-ahead."""
    if now is None:
        now = datetime.now(timezone.utc)
    return [c for c in candles if c.close_time <= now]


def validate_series(candles: list[Candle], timeframe: str) -> list[SeriesGap]:
    """Validate a candle series and return gaps (missing intervals). Gaps are
    REPORTED, not filled. Raises ValueError on hard defects: wrong bar duration,
    non-strictly-increasing order, duplicates, or overlaps.
    """
    expected = timedelta(minutes=timeframe_minutes(timeframe))
    enforce_grid = timeframe in _EPOCH_ALIGNED_TFS
    gaps: list[SeriesGap] = []
    for i, c in enumerate(candles):
        if enforce_grid and not is_grid_aligned(c.open_time, timeframe):
            raise ValueError(
                f"bar {i} open_time {c.open_time.isoformat()} is off the {timeframe} grid "
                f"(phase-shifted buckets — not scheduler-compatible)"
            )
        if c.close_time - c.open_time != expected:
            raise ValueError(f"bar {i} duration {c.close_time - c.open_time} != {expected}")
        if i == 0:
            continue
        prev = candles[i - 1]
        if c.open_time <= prev.open_time:
            raise ValueError(f"bar {i} not strictly after previous (duplicate/disorder)")
        if c.open_time < prev.close_time:
            raise ValueError(f"bar {i} overlaps previous bar")
        if c.open_time > prev.close_time:
            missing = round((c.open_time - prev.close_time) / expected)
            gaps.append(SeriesGap(after=prev.close_time, before=c.open_time, missing_bars=missing))
    return gaps
