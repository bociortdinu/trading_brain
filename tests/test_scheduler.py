"""Scheduler: target selection from ACTUAL provider bars + resilient tick."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.jobs import WindowCache, floor_m15, next_m15, safe_catch_up, select_targets
from config.settings import Settings
from data_collector.providers.base import Candle
from data_collector.providers.polygon import ProviderError
from tests.helpers import run

UTC = timezone.utc
M15 = timedelta(minutes=15)


def _closes(start: datetime, n: int) -> list[datetime]:
    return [start + M15 * (i + 1) for i in range(n)]


def test_floor_and_next_m15():
    assert floor_m15(datetime(2026, 7, 10, 21, 7, 30, tzinfo=UTC)) == datetime(2026, 7, 10, 21, 0, tzinfo=UTC)
    assert next_m15(datetime(2026, 7, 10, 21, 7, tzinfo=UTC)) == datetime(2026, 7, 10, 21, 15, tzinfo=UTC)


def test_first_run_takes_latest_only():
    closes = _closes(datetime(2026, 7, 10, 20, 0, tzinfo=UTC), 5)
    assert select_targets(closes, None, 8) == [closes[-1]]  # not all history


def test_weekend_restart_no_new_bars():
    # Friday session closes; Sunday restart -> provider still ends at Friday's last bar.
    friday = _closes(datetime(2026, 7, 10, 20, 0, tzinfo=UTC), 5)
    assert select_targets(friday, friday[-1], 8) == []  # nothing to process over the weekend


def test_market_closed_provider_ends_at_last_session():
    closes = _closes(datetime(2026, 7, 10, 20, 0, tzinfo=UTC), 3)
    # last stored is the 2nd bar; only the truly-newer provider bar is a target
    assert select_targets(closes, closes[1], 8) == [closes[2]]


def test_late_published_bar_processed_once():
    closes = _closes(datetime(2026, 7, 12, 22, 0, tzinfo=UTC), 3)
    assert select_targets(closes, closes[1], 8) == [closes[2]]     # newly published bar
    assert select_targets(closes, closes[2], 8) == []              # not reprocessed


def test_backfill_capped_to_most_recent():
    closes = _closes(datetime(2026, 7, 12, 0, 0, tzinfo=UTC), 20)
    got = select_targets(closes, closes[0], 4)
    assert got == closes[-4:]


def test_no_bars_available():
    assert select_targets([], datetime(2026, 7, 12, tzinfo=UTC), 8) == []


# ---- resilience: transient swallowed, unexpected escalated ---- #
def _settings() -> Settings:
    return Settings(_env_file=None, db_dsn="postgresql://x/y", polygon_api_key="k")


def test_safe_catch_up_swallows_transient_provider_outage():
    class _Transient:
        async def get_ohlcv(self, *a, **k):
            raise ProviderError("provider temporarily unavailable")

    res = run(safe_catch_up(_settings(), _Transient(), "polygon"))
    assert "error" in res  # returned, not raised -> loop survives


def test_safe_catch_up_escalates_unexpected_error():
    class _Bug:
        async def get_ohlcv(self, *a, **k):
            raise KeyError("programming bug")  # not a known transient

    with pytest.raises(KeyError):  # escalated, not hidden
        run(safe_catch_up(_settings(), _Bug(), "polygon"))


# ---- window cache: reuse higher timeframes between M15 ticks ---- #
def _one_candle(close_time, tf_min):
    return Candle(open_time=close_time - M15 * (tf_min // 15 or 1), close_time=close_time,
                  open=100.0, high=101.0, low=99.0, close=100.5)


class _CountingProvider:
    def __init__(self, close_time):
        self.calls: dict[str, int] = {}
        self._ct = close_time

    async def get_ohlcv(self, symbol, tf, bars):
        from data_collector.providers.base import timeframe_minutes
        self.calls[tf] = self.calls.get(tf, 0) + 1
        return [_one_candle(self._ct, timeframe_minutes(tf))]


def test_window_cache_reuses_until_new_bar():
    ct = datetime(2026, 7, 10, 20, 0, tzinfo=UTC)   # last 1h bar close
    p = _CountingProvider(ct)
    cache = WindowCache()
    run(cache.window(p, "X", "1h", 10, ct + timedelta(minutes=1)))   # fetch
    run(cache.window(p, "X", "1h", 10, ct + timedelta(minutes=5)))   # reuse (still within the hour)
    run(cache.window(p, "X", "1h", 10, ct + timedelta(minutes=14)))  # reuse
    assert p.calls["1h"] == 1
    run(cache.window(p, "X", "1h", 10, ct + timedelta(minutes=61)))  # new 1h bar closed -> refetch
    assert p.calls["1h"] == 2
