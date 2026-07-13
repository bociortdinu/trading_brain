"""Market session model + gap classification, PROVIDER + INSTRUMENT specific.

Sessions are expressed in a DST-aware timezone (not hardcoded UTC hours) so the
daily break and weekly open/close move correctly across DST. Holidays and early
closes are a VERSIONED list of CONFIRMED exceptions only — an unexplained gap
(e.g. 2026-05-07) is NOT auto-labelled a holiday; it stays "unexpected".

Two distinct uses (see features/eligibility.py):
- `timeframe_quality` -> full-window data quality for AUDIT (classify every gap);
- eligibility uses only a recent window (a stale old holiday must not block "now").

Boundaries below are for Polygon `C:XAUUSD`, derived from the observed feed and
tagged with a calendar version. Other instruments get their own calendar.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from .providers.base import SeriesGap, timeframe_minutes

EXPECTED_SESSION_GAP = "expected_session_gap"
UNEXPECTED_MISSING_BAR = "unexpected_missing_bar"

_NY = ZoneInfo("America/New_York")

# Calendar identity — bump when boundaries or exceptions change (reproducibility).
XAUUSD_CALENDAR_VERSION = "xauusd-polygon-2026.1"

# CONFIRMED exceptions only. Values: "closed" | ("early_close", et_hour).
# 2026-07-03: US Independence Day (observed) — early close confirmed from the
# C:XAUUSD feed (data ends ~14:00 ET that day).
_XAUUSD_EXCEPTIONS: dict[date, object] = {
    date(2026, 7, 3): ("early_close", 14),
}


class XauUsdCalendar:
    """Gold / FX session in America/New_York: week Sun 17:00 -> Fri 17:00 ET,
    daily maintenance break 17:00-18:00 ET, plus confirmed exceptions."""

    version = XAUUSD_CALENDAR_VERSION

    def is_open(self, dt_utc: datetime) -> bool:
        et = dt_utc.astimezone(_NY)
        exc = _XAUUSD_EXCEPTIONS.get(et.date())
        if exc == "closed":
            return False
        if isinstance(exc, tuple) and exc[0] == "early_close" and et.hour >= exc[1]:
            return False
        wd = et.weekday()  # Mon=0 .. Sun=6
        if wd == 5:  # Saturday
            return False
        if wd == 6:  # Sunday: opens 17:00 ET
            return et.hour >= 17
        if wd == 4 and et.hour >= 17:  # Friday close
            return False
        if et.hour == 17:  # daily maintenance break, Mon-Thu 17:00-18:00 ET
            return False
        return True


DEFAULT_CALENDAR = XauUsdCalendar()


def classify_gap(gap: SeriesGap, timeframe: str, calendar: XauUsdCalendar = DEFAULT_CALENDAR) -> str:
    """A gap is unexpected if ANY missing bar would have opened while the market was open."""
    from datetime import timedelta

    step = timedelta(minutes=timeframe_minutes(timeframe))
    t = gap.after  # open_time of the first missing bar (== prev bar's close_time)
    while t < gap.before:
        if calendar.is_open(t):
            return UNEXPECTED_MISSING_BAR
        t += step
    return EXPECTED_SESSION_GAP


def timeframe_quality(
    bars: int, min_bars: int, gaps: list[SeriesGap], timeframe: str,
    calendar: XauUsdCalendar = DEFAULT_CALENDAR,
) -> dict:
    """FULL-window audit quality for one timeframe (classifies every gap)."""
    classified = [
        {
            "after": g.after.isoformat(),
            "before": g.before.isoformat(),
            "missing_bars": g.missing_bars,
            "kind": classify_gap(g, timeframe, calendar),
        }
        for g in gaps
    ]
    has_unexpected = any(c["kind"] == UNEXPECTED_MISSING_BAR for c in classified)
    if bars < min_bars:
        verdict = "insufficient"
    elif has_unexpected:
        verdict = "degraded"
    elif classified:
        verdict = "expected_gaps"
    else:
        verdict = "ok"
    return {"bars": bars, "verdict": verdict, "calendar_version": calendar.version, "gaps": classified}
