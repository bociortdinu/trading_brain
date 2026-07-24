"""DST-aware C:XAUUSD calendar + gap classification (full-window audit)."""

from __future__ import annotations

from datetime import datetime, timezone

from data_collector.providers.base import SeriesGap
from data_collector.session import (
    DEFAULT_CALENDAR,
    EXPECTED_SESSION_GAP,
    UNEXPECTED_MISSING_BAR,
    classify_gap,
    timeframe_quality,
)

UTC = timezone.utc
cal = DEFAULT_CALENDAR


def test_open_hours_in_et():
    assert cal.is_open(datetime(2026, 7, 8, 16, tzinfo=UTC))       # Wed 12:00 EDT -> open
    assert not cal.is_open(datetime(2026, 7, 11, 12, tzinfo=UTC))  # Saturday -> closed
    assert not cal.is_open(datetime(2026, 7, 12, 20, tzinfo=UTC))  # Sun 16:00 EDT -> closed
    assert cal.is_open(datetime(2026, 7, 12, 22, tzinfo=UTC))      # Sun 18:00 EDT -> open


def test_dst_shifts_the_daily_break():
    # Daily break is 17:00 ET. Its UTC hour MOVES with DST.
    # Summer (EDT, UTC-4): 17:00 ET == 21:00 UTC
    assert not cal.is_open(datetime(2026, 7, 8, 21, tzinfo=UTC))   # Wed 17:00 EDT -> break
    assert cal.is_open(datetime(2026, 7, 8, 20, tzinfo=UTC))       # Wed 16:00 EDT -> open
    # Winter (EST, UTC-5): 17:00 ET == 22:00 UTC
    assert not cal.is_open(datetime(2026, 1, 7, 22, tzinfo=UTC))   # Wed 17:00 EST -> break
    assert cal.is_open(datetime(2026, 1, 7, 21, tzinfo=UTC))       # Wed 16:00 EST -> open


def test_confirmed_early_close_only():
    # 2026-07-03 (US July 4th observed): confirmed early close 14:00 ET (18:00 UTC EDT).
    assert cal.is_open(datetime(2026, 7, 3, 17, tzinfo=UTC))       # 13:00 EDT -> still open
    assert not cal.is_open(datetime(2026, 7, 3, 18, 30, tzinfo=UTC))  # 14:30 EDT -> closed


def test_unexplained_gap_is_not_auto_holiday():
    # 2026-05-07 (Thu) is NOT in the exceptions -> a missing bar stays UNEXPECTED.
    gap = SeriesGap(after=datetime(2026, 5, 7, 12, tzinfo=UTC),
                    before=datetime(2026, 5, 7, 13, tzinfo=UTC), missing_bars=1)
    assert classify_gap(gap, "1h") == UNEXPECTED_MISSING_BAR


def test_daily_break_and_weekend_are_expected():
    daily = SeriesGap(after=datetime(2026, 7, 8, 21, tzinfo=UTC),
                      before=datetime(2026, 7, 8, 22, tzinfo=UTC), missing_bars=4)
    weekend = SeriesGap(after=datetime(2026, 7, 10, 21, tzinfo=UTC),
                        before=datetime(2026, 7, 12, 21, tzinfo=UTC), missing_bars=192)
    assert classify_gap(daily, "15min") == EXPECTED_SESSION_GAP
    assert classify_gap(weekend, "15min") == EXPECTED_SESSION_GAP


def test_intraday_gap_is_unexpected():
    gap = SeriesGap(after=datetime(2026, 7, 8, 15, tzinfo=UTC),
                    before=datetime(2026, 7, 8, 16, tzinfo=UTC), missing_bars=3)
    assert classify_gap(gap, "15min") == UNEXPECTED_MISSING_BAR


def test_xtb_calendar_sunday_open_differs_from_polygon():
    from data_collector.session import POLYGON_XAUUSD_CALENDAR, XTB_XAUUSD_CALENDAR, calendar_for

    # Sunday 21:00 UTC (17:00 EDT): Polygon considers it OPEN, XTB still CLOSED (opens 18:00 ET).
    sun_2100 = datetime(2026, 7, 12, 21, tzinfo=UTC)
    assert POLYGON_XAUUSD_CALENDAR.is_open(sun_2100)
    assert not XTB_XAUUSD_CALENDAR.is_open(sun_2100)
    assert XTB_XAUUSD_CALENDAR.is_open(datetime(2026, 7, 12, 22, tzinfo=UTC))  # 18:00 EDT -> open
    assert calendar_for("xtb").version == "xauusd-xtb-2026.1"
    assert calendar_for("polygon").version == "xauusd-polygon-2026.1"


def test_xtb_daily_break_and_friday_close_match_et_rollover():
    from data_collector.session import XTB_XAUUSD_CALENDAR as cal

    assert not cal.is_open(datetime(2026, 7, 8, 21, tzinfo=UTC))   # Wed 17:00 EDT -> break
    assert cal.is_open(datetime(2026, 7, 8, 22, tzinfo=UTC))       # Wed 18:00 EDT -> open
    assert not cal.is_open(datetime(2026, 7, 10, 21, tzinfo=UTC))  # Fri 17:00 EDT -> weekly close


def test_calendar_for_unknown_provider_fails_closed():
    from data_collector.session import calendar_for

    try:
        calendar_for("mystery")
        assert False, "expected fail-closed"
    except ValueError:
        pass


def test_timeframe_quality_verdicts():
    weekend = SeriesGap(after=datetime(2026, 7, 10, 21, tzinfo=UTC),
                        before=datetime(2026, 7, 12, 21, tzinfo=UTC), missing_bars=192)
    intraday = SeriesGap(after=datetime(2026, 7, 8, 15, tzinfo=UTC),
                         before=datetime(2026, 7, 8, 16, tzinfo=UTC), missing_bars=3)
    assert timeframe_quality(250, 200, [], "15min")["verdict"] == "ok"
    assert timeframe_quality(250, 200, [weekend], "15min")["verdict"] == "expected_gaps"
    assert timeframe_quality(250, 200, [intraday], "15min")["verdict"] == "degraded"
    assert timeframe_quality(50, 200, [], "15min")["verdict"] == "insufficient"
    assert timeframe_quality(250, 200, [], "15min")["calendar_version"] == cal.version
