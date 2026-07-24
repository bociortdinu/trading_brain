"""Bar-archive harvest: merge semantics, fail-closed conflicts, atomic writes, and a
round-trip proving the archive is readable by the provider the backtest actually uses."""

from __future__ import annotations

import csv
import pathlib
from datetime import datetime, timedelta, timezone

import pytest

from app.harvest import (
    CSV_FIELDS,
    HarvestConflict,
    evaluable_bars,
    harvest,
    merge_bars,
    read_archive,
    write_archive,
)
from features.engineering import MIN_BARS
from config.settings import Settings
from data_collector.providers.base import Candle
from data_collector.providers.csv_provider import CsvMarketDataProvider
from tests import synthetic
from tests.helpers import run


class FakeProvider:
    """Serves a fixed window per timeframe, like a rolling-window feed would."""

    def __init__(self, windows: dict[str, list[Candle]]) -> None:
        self.windows = windows
        self.closed = False
        self.calls: list[tuple[str, str, int]] = []

    async def get_ohlcv(self, symbol: str, timeframe: str, count: int) -> list[Candle]:
        self.calls.append((symbol, timeframe, count))
        return self.windows[timeframe][-count:]

    async def aclose(self) -> None:
        self.closed = True


def _settings(**kw) -> Settings:
    base = dict(symbol_query="GOLD", market_data_provider="csv", csv_dir="/unused",
                timeframes=["15min", "1h"], db_dsn="postgresql://u:p@127.0.0.1:5432/x")
    base.update(kw)
    return Settings(**base)


def _rows(path: pathlib.Path) -> list[dict[str, str]]:
    with path.open(newline="") as fh:
        return list(csv.DictReader(fh))


# ---- merge semantics ---- #
def test_merge_into_empty_archive_keeps_order():
    bars = synthetic.trend(n=5)
    rows, added, conflicts = merge_bars({}, bars)
    assert added == 5 and conflicts == []
    assert [r["open_time"] for r in rows] == [b.open_time.isoformat() for b in bars]


def test_merge_is_idempotent():
    bars = synthetic.trend(n=5)
    rows, _, _ = merge_bars({}, bars)
    existing = {datetime.fromisoformat(r["open_time"]): r for r in rows}
    rows2, added, conflicts = merge_bars(existing, bars)
    assert added == 0 and conflicts == [] and rows2 == rows


def test_merge_accumulates_beyond_the_providers_rolling_window():
    """The whole point: an old window already archived plus a newer window that no longer
    overlaps it must yield the UNION, not just what the provider can still serve."""
    old = synthetic.trend(n=10)
    new = synthetic.trend(n=10, start=old[-1].open_time + timedelta(minutes=15))
    rows, _, _ = merge_bars({}, old)
    existing = {datetime.fromisoformat(r["open_time"]): r for r in rows}
    merged, added, _ = merge_bars(existing, new)
    assert added == 10
    assert len(merged) == 20
    times = [r["open_time"] for r in merged]
    assert times == sorted(times)


def test_merge_sorts_out_of_order_arrival():
    bars = synthetic.trend(n=6)
    rows, _, _ = merge_bars({}, list(reversed(bars)))
    assert [r["open_time"] for r in rows] == [b.open_time.isoformat() for b in bars]


def test_equal_values_written_differently_are_not_a_conflict():
    bar = synthetic.trend(n=1)[0]
    existing = {bar.open_time: {"open_time": bar.open_time.isoformat(),
                                "close_time": bar.close_time.isoformat(),
                                "open": f"{bar.open:.4f}", "high": f"{bar.high:.4f}",
                                "low": f"{bar.low:.4f}", "close": f"{bar.close:.4f}",
                                "volume": f"{bar.volume:.4f}"}}
    _, added, conflicts = merge_bars(existing, [bar])
    assert added == 0 and conflicts == []


# ---- fail-closed conflict handling ---- #
def _conflicting(bar: Candle) -> Candle:
    return Candle(open_time=bar.open_time, close_time=bar.close_time, open=bar.open,
                  high=bar.high + 5.0, low=bar.low, close=bar.close, volume=bar.volume)


def test_conflict_fails_closed_by_default():
    bars = synthetic.trend(n=3)
    rows, _, _ = merge_bars({}, bars)
    existing = {datetime.fromisoformat(r["open_time"]): r for r in rows}
    with pytest.raises(HarvestConflict) as exc:
        merge_bars(existing, [_conflicting(bars[1])])
    assert bars[1].open_time.isoformat() in str(exc.value)


def test_conflict_keep_preserves_the_archived_bar():
    bars = synthetic.trend(n=3)
    rows, _, _ = merge_bars({}, bars)
    existing = {datetime.fromisoformat(r["open_time"]): r for r in rows}
    merged, added, conflicts = merge_bars(existing, [_conflicting(bars[1])], on_conflict="keep")
    assert added == 0 and conflicts == [bars[1].open_time]
    assert float(merged[1]["high"]) == pytest.approx(bars[1].high)


def test_conflict_replace_takes_the_provider_bar():
    bars = synthetic.trend(n=3)
    rows, _, _ = merge_bars({}, bars)
    existing = {datetime.fromisoformat(r["open_time"]): r for r in rows}
    merged, _, conflicts = merge_bars(existing, [_conflicting(bars[1])], on_conflict="replace")
    assert conflicts == [bars[1].open_time]
    assert float(merged[1]["high"]) == pytest.approx(bars[1].high + 5.0)


def test_failed_harvest_leaves_the_archive_untouched(tmp_path):
    """A conflict must abort BEFORE any file is rewritten."""
    bars = synthetic.trend(n=4, tf_min=15)
    provider = FakeProvider({"15min": bars})
    st = _settings(timeframes=["15min"])
    run(harvest(st, str(tmp_path), 10, "fail", provider=provider))
    before = (tmp_path / "GOLD_15min.csv").read_bytes()

    provider.windows["15min"] = [_conflicting(bars[2])]
    with pytest.raises(HarvestConflict):
        run(harvest(st, str(tmp_path), 10, "fail", provider=provider))
    assert (tmp_path / "GOLD_15min.csv").read_bytes() == before


# ---- durability ---- #
def test_write_is_atomic_and_leaves_no_temp_files(tmp_path):
    path = tmp_path / "GOLD_15min.csv"
    rows, _, _ = merge_bars({}, synthetic.trend(n=3))
    write_archive(path, rows)
    assert path.exists()
    assert [p.name for p in tmp_path.iterdir()] == ["GOLD_15min.csv"]


def test_write_creates_missing_directories(tmp_path):
    path = tmp_path / "deep" / "nested" / "GOLD_1h.csv"
    rows, _, _ = merge_bars({}, synthetic.trend(n=2, tf_min=60))
    write_archive(path, rows)
    assert path.exists()


def test_read_archive_of_missing_file_is_empty(tmp_path):
    assert read_archive(tmp_path / "nope.csv") == {}


def test_header_matches_the_csv_provider_contract(tmp_path):
    path = tmp_path / "GOLD_15min.csv"
    rows, _, _ = merge_bars({}, synthetic.trend(n=2))
    write_archive(path, rows)
    with path.open(newline="") as fh:
        assert next(csv.reader(fh)) == CSV_FIELDS


# ---- end-to-end ---- #
def test_harvest_writes_one_file_per_timeframe_and_closes_the_provider(tmp_path):
    provider = FakeProvider({"15min": synthetic.trend(n=20, tf_min=15),
                             "1h": synthetic.trend(n=20, tf_min=60)})
    # An injected provider is owned by the caller, so harvest must NOT close it.
    run(harvest(_settings(), str(tmp_path), 20, "fail", provider=provider))
    assert (tmp_path / "GOLD_15min.csv").exists()
    assert (tmp_path / "GOLD_1h.csv").exists()
    assert not provider.closed


def test_harvested_archive_is_readable_by_the_csv_provider(tmp_path):
    """The archive must feed the exact provider the backtest uses, with bars intact."""
    bars = synthetic.trend(n=30, tf_min=15)
    provider = FakeProvider({"15min": bars})
    run(harvest(_settings(timeframes=["15min"]), str(tmp_path), 30, "fail", provider=provider))

    loaded = run(CsvMarketDataProvider(tmp_path).get_ohlcv("GOLD", "15min", 30))
    assert len(loaded) == 30
    for original, restored in zip(bars, loaded):
        assert restored.open_time == original.open_time
        assert restored.close_time == original.close_time
        assert restored.open == pytest.approx(original.open)
        assert restored.high == pytest.approx(original.high)
        assert restored.low == pytest.approx(original.low)
        assert restored.close == pytest.approx(original.close)
        assert restored.volume == pytest.approx(original.volume)


def test_float_values_survive_the_round_trip_exactly(tmp_path):
    """Bit-exact floats matter: the dataset content hash must be stable across a save/load."""
    ot = datetime(2026, 3, 1, tzinfo=timezone.utc)
    bar = Candle(open_time=ot, close_time=ot + timedelta(minutes=15),
                 open=2000.123456789012, high=2000.987654321098, low=1999.111111111111,
                 close=2000.555555555555, volume=1234.6789012345)
    provider = FakeProvider({"15min": [bar]})
    run(harvest(_settings(timeframes=["15min"]), str(tmp_path), 1, "fail", provider=provider))

    loaded = run(CsvMarketDataProvider(tmp_path).get_ohlcv("GOLD", "15min", 1))[0]
    assert loaded.open == bar.open and loaded.high == bar.high
    assert loaded.low == bar.low and loaded.close == bar.close
    assert loaded.volume == bar.volume


# ---- backtest-readiness diagnostic ---- #
def _archive(n: int, tf_min: int, end: datetime) -> list[dict[str, str]]:
    start = end - timedelta(minutes=tf_min * n)
    rows, _, _ = merge_bars({}, synthetic.trend(n=n, tf_min=tf_min, start=start))
    return rows


def test_evaluable_bars_flags_the_binding_timeframe():
    """The daily is short, so nothing is evaluable even though M15 looks plentiful — this is
    the silent `bars_evaluated=0` failure, made explicit."""
    end = datetime(2026, 6, 1, tzinfo=timezone.utc)
    archives = {"15min": _archive(2600, 15, end), "1day": _archive(120, 1440, end)}
    usable, binding = evaluable_bars(archives)
    assert usable == 0 and binding == "1day"


def test_evaluable_bars_counts_only_bars_past_every_warmup():
    """With a deep daily archive the daily warmup ends long before the M15 window opens, so
    the trigger's OWN warmup becomes the binding constraint."""
    end = datetime(2026, 6, 1, tzinfo=timezone.utc)
    archives = {"15min": _archive(2600, 15, end), "1day": _archive(500, 1440, end)}
    usable, binding = evaluable_bars(archives)
    assert usable == 2600 - MIN_BARS + 1 and binding == "15min"


def test_a_short_daily_binds_before_the_triggers_own_warmup():
    """Same M15 archive, shallower daily -> the daily now binds and fewer bars are evaluable."""
    end = datetime(2026, 6, 1, tzinfo=timezone.utc)
    deep = {"15min": _archive(2600, 15, end), "1day": _archive(500, 1440, end)}
    shallow = {"15min": _archive(2600, 15, end), "1day": _archive(220, 1440, end)}
    assert evaluable_bars(shallow)[1] == "1day"
    assert evaluable_bars(shallow)[0] < evaluable_bars(deep)[0]


def test_evaluable_bars_excludes_the_triggers_own_warmup():
    end = datetime(2026, 6, 1, tzinfo=timezone.utc)
    archives = {"15min": _archive(250, 15, end)}
    usable, binding = evaluable_bars(archives)
    assert usable == 250 - MIN_BARS + 1 and binding == "15min"


def test_harvest_returns_nonzero_when_the_archive_is_not_backtest_ready(tmp_path):
    end = datetime(2026, 6, 1, tzinfo=timezone.utc)
    provider = FakeProvider({
        "15min": synthetic.trend(n=600, tf_min=15, start=end - timedelta(minutes=15 * 600)),
        "1day": synthetic.trend(n=20, tf_min=1440, start=end - timedelta(minutes=1440 * 20)),
    })
    rc = run(harvest(_settings(timeframes=["15min", "1day"]), str(tmp_path), 600, "fail",
                     provider=provider))
    assert rc == 1  # refuses to claim readiness it does not have


def test_harvest_returns_zero_when_backtest_ready(tmp_path):
    end = datetime(2026, 6, 1, tzinfo=timezone.utc)
    provider = FakeProvider({
        "15min": synthetic.trend(n=600, tf_min=15, start=end - timedelta(minutes=15 * 600)),
        "1day": synthetic.trend(n=500, tf_min=1440, start=end - timedelta(minutes=1440 * 500)),
    })
    rc = run(harvest(_settings(timeframes=["15min", "1day"]), str(tmp_path), 600, "fail",
                     provider=provider))
    assert rc == 0


def test_second_harvest_extends_the_archive_on_disk(tmp_path):
    """Simulates the real operational loop: harvest today, harvest again later, keep both."""
    st = _settings(timeframes=["15min"])
    first = synthetic.trend(n=10, tf_min=15)
    run(harvest(st, str(tmp_path), 10, "fail", provider=FakeProvider({"15min": first})))

    later = synthetic.trend(n=10, tf_min=15, start=first[-1].open_time + timedelta(minutes=15))
    run(harvest(st, str(tmp_path), 10, "fail", provider=FakeProvider({"15min": later})))

    rows = _rows(tmp_path / "GOLD_15min.csv")
    assert len(rows) == 20
    times = [r["open_time"] for r in rows]
    assert times == sorted(times) and len(set(times)) == 20
