"""Scheduler: target selection from ACTUAL provider bars + resilient tick."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.jobs import (
    WindowCache,
    floor_m15,
    next_m15,
    observed_catch_up,
    safe_catch_up,
    select_targets,
    should_observe_spread,
)
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


def test_replay_never_observes_a_live_quote():
    """REGRESSION: the scheduler used to attach the wall-clock quote to the LATEST bar whatever
    the mode, so a replayed bar could be recorded with a price from its own future. A quote
    describes NOW: latest bar + online only. Fail-closed on anything else."""
    assert should_observe_spread("online", True) is True       # the only case that may observe
    assert should_observe_spread("replay", True) is False      # <- the contamination bug
    assert should_observe_spread("online", False) is False     # backfilled bar: never
    assert should_observe_spread("replay", False) is False
    assert should_observe_spread("", True) is False            # unknown mode -> fail closed


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


class _Telemetry:
    def __init__(self):
        self.events = []

    def start_run(self, kind, **kwargs):
        self.events.append(("start", kind, kwargs))
        return 17

    def finish_run(self, run_id, status, **kwargs):
        self.events.append(("finish", run_id, status, kwargs))

    def heartbeat(self, status, **kwargs):
        self.events.append(("heartbeat", status, kwargs))


def test_observed_tick_records_success(monkeypatch):
    async def fake_catch_up(*args, **kwargs):
        return {"inserted": 2, "unchanged": 1}

    monkeypatch.setattr("app.jobs.catch_up", fake_catch_up)
    telemetry = _Telemetry()
    result = run(observed_catch_up(_settings(), object(), "polygon", telemetry))
    assert result == {"inserted": 2, "unchanged": 1}
    finish = next(e for e in telemetry.events if e[0] == "finish")
    assert finish[2] == "success"
    assert finish[3]["bars_processed"] == 3


def test_observed_tick_records_transient_error(monkeypatch):
    async def fake_catch_up(*args, **kwargs):
        raise ProviderError("temporary?apiKey=must-not-be-persisted")

    monkeypatch.setattr("app.jobs.catch_up", fake_catch_up)
    telemetry = _Telemetry()
    result = run(observed_catch_up(_settings(), object(), "polygon", telemetry))
    assert "error" in result
    assert next(e for e in telemetry.events if e[0] == "finish")[2] == "transient_error"


def test_observed_tick_records_and_escalates_bug(monkeypatch):
    async def fake_catch_up(*args, **kwargs):
        raise KeyError("bug")

    monkeypatch.setattr("app.jobs.catch_up", fake_catch_up)
    telemetry = _Telemetry()
    with pytest.raises(KeyError):
        run(observed_catch_up(_settings(), object(), "polygon", telemetry))
    assert next(e for e in telemetry.events if e[0] == "finish")[2] == "failed"


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
