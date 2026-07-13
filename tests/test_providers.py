"""CSV provider + news as_of discipline."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from data_collector.news.base import NewsItem, as_of_filter
from data_collector.providers.base import Candle, validate_series
from data_collector.providers.csv_provider import CsvMarketDataProvider
from tests.helpers import run


def _c(o_iso, c_iso, o=100.0, h=101.0, low=99.0, close=100.5):
    return Candle(open_time=datetime.fromisoformat(o_iso), close_time=datetime.fromisoformat(c_iso),
                  open=o, high=h, low=low, close=close, volume=1.0)


# ---- Candle validation ---- #
def test_candle_rejects_naive_timestamp():
    with pytest.raises(Exception):
        Candle(open_time=datetime(2026, 1, 1), close_time=datetime(2026, 1, 1, 0, 15),
               open=1, high=2, low=0.5, close=1.5)


def test_candle_rejects_incoherent_ohlc():
    with pytest.raises(Exception):
        _c("2026-01-01T00:00:00+00:00", "2026-01-01T00:15:00+00:00", h=100.0, close=105.0)  # high < close


def test_candle_rejects_nonpositive():
    with pytest.raises(Exception):
        _c("2026-01-01T00:00:00+00:00", "2026-01-01T00:15:00+00:00", low=-1.0)


def test_candle_rejects_close_before_open():
    with pytest.raises(Exception):
        _c("2026-01-01T00:15:00+00:00", "2026-01-01T00:00:00+00:00")


# ---- series validation ---- #
def test_validate_series_ok_no_gaps():
    candles = [
        _c("2026-01-01T00:00:00+00:00", "2026-01-01T00:15:00+00:00"),
        _c("2026-01-01T00:15:00+00:00", "2026-01-01T00:30:00+00:00"),
    ]
    assert validate_series(candles, "15min") == []


def test_validate_series_reports_gap_without_filling():
    candles = [
        _c("2026-01-01T00:00:00+00:00", "2026-01-01T00:15:00+00:00"),
        _c("2026-01-01T01:00:00+00:00", "2026-01-01T01:15:00+00:00"),  # 3 bars missing
    ]
    gaps = validate_series(candles, "15min")
    assert len(gaps) == 1 and gaps[0].missing_bars == 3
    assert len(candles) == 2  # NOT forward-filled


def test_validate_series_rejects_wrong_duration():
    with pytest.raises(ValueError):
        validate_series([_c("2026-01-01T00:00:00+00:00", "2026-01-01T00:20:00+00:00")], "15min")


def test_validate_series_rejects_disorder():
    candles = [
        _c("2026-01-01T00:15:00+00:00", "2026-01-01T00:30:00+00:00"),
        _c("2026-01-01T00:00:00+00:00", "2026-01-01T00:15:00+00:00"),
    ]
    with pytest.raises(ValueError):
        validate_series(candles, "15min")


def _write_csv(path, rows):
    path.write_text(
        "open_time,close_time,open,high,low,close,volume\n"
        + "\n".join(rows)
    )


def test_csv_provider_reads_candles(tmp_path):
    _write_csv(
        tmp_path / "GOLD_15min.csv",
        [
            "2026-01-01T00:00:00+00:00,2026-01-01T00:15:00+00:00,2000,2001,1999,2000.5,10",
            "2026-01-01T00:15:00+00:00,2026-01-01T00:30:00+00:00,2000.5,2002,2000,2001.5,12",
        ],
    )
    provider = CsvMarketDataProvider(tmp_path)
    candles = run(provider.get_ohlcv("GOLD", "15min", 10))
    assert len(candles) == 2
    assert candles[0].close == 2000.5
    assert candles[-1].high == 2002.0


def test_csv_provider_count_limit(tmp_path):
    rows = [
        f"2026-01-01T{h:02d}:00:00+00:00,2026-01-01T{h:02d}:15:00+00:00,1,2,0.5,1.5,1"
        for h in range(5)
    ]
    _write_csv(tmp_path / "GOLD_15min.csv", rows)
    candles = run(CsvMarketDataProvider(tmp_path).get_ohlcv("GOLD", "15min", 2))
    assert len(candles) == 2


def test_csv_provider_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        run(CsvMarketDataProvider(tmp_path).get_ohlcv("GOLD", "1h", 10))


def _news(pub, ing, headline="x"):
    return NewsItem(source="s", external_id=headline, publication_time=pub,
                    ingestion_time=ing, headline=headline)


def test_news_as_of_filters_future_publication():
    t = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)
    future = datetime(2026, 7, 12, 13, 0, tzinfo=timezone.utc)
    past = datetime(2026, 7, 12, 11, 0, tzinfo=timezone.utc)
    items = [_news(past, past, "past"), _news(future, future, "future")]
    kept = as_of_filter(items, t)
    assert [it.headline for it in kept] == ["past"]


def test_news_as_of_filters_late_ingestion():
    # published before as_of, but ingested after -> leakage; must be dropped
    t = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)
    pub = datetime(2026, 7, 12, 11, 0, tzinfo=timezone.utc)
    late_ingest = datetime(2026, 7, 12, 13, 0, tzinfo=timezone.utc)
    assert as_of_filter([_news(pub, late_ingest)], t) == []
