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


class SessionCalendar:
    """DST-aware gold/FX session calendar, PARAMETERIZED by its local session timezone and
    boundary hours so that different SOURCES can carry their own boundaries. The same
    instrument on two providers can have different UTC boundaries — a broker anchors the
    trading day to its OWN server time, so e.g. the daily rollover can differ by ~1h from a
    data vendor that expresses it in America/New_York. Hence the calendar is provider-specific
    and versioned; `calendar_for(provider)` selects it.

    Boundaries (local `tz` hours): week opens Sun `open_hour`, closes Fri `close_hour`; daily
    maintenance break at `break_hour` (1h, Mon-Thu). `exceptions` are CONFIRMED only.
    """

    def __init__(self, *, version: str, tz: ZoneInfo, open_hour: int, close_hour: int,
                 break_hour: int, exceptions: dict[date, object] | None = None) -> None:
        self.version = version
        self._tz = tz
        self._open_hour = open_hour
        self._close_hour = close_hour
        self._break_hour = break_hour
        self._exceptions = exceptions or {}

    def is_open(self, dt_utc: datetime) -> bool:
        lt = dt_utc.astimezone(self._tz)
        exc = self._exceptions.get(lt.date())
        if exc == "closed":
            return False
        if isinstance(exc, tuple) and exc[0] == "early_close" and lt.hour >= exc[1]:
            return False
        wd = lt.weekday()  # Mon=0 .. Sun=6
        if wd == 5:  # Saturday
            return False
        if wd == 6:  # Sunday: opens at open_hour
            return lt.hour >= self._open_hour
        if wd == 4 and lt.hour >= self._close_hour:  # Friday close
            return False
        if lt.hour == self._break_hour:  # daily maintenance break (1h), Mon-Thu
            return False
        return True


# Polygon C:XAUUSD — validated: week Sun 17:00 -> Fri 17:00 ET, daily break 17:00-18:00 ET.
POLYGON_XAUUSD_CALENDAR = SessionCalendar(
    version=XAUUSD_CALENDAR_VERSION, tz=_NY, open_hour=17, close_hour=17, break_hour=17,
    exceptions=_XAUUSD_EXCEPTIONS,
)

# Backward-compatible aliases (existing code/tests import these).
XauUsdCalendar = SessionCalendar
DEFAULT_CALENDAR = POLYGON_XAUUSD_CALENDAR

# Provider -> validated calendar. XTB's boundaries differ (its D1 rolls at 22:00 UTC in
# summer vs Polygon's 21:00 UTC) and must be DERIVED from live XTB gaps before being added
# here; until then calendar_for('xtb') fails closed rather than silently using Polygon's.
_CALENDARS: dict[str, SessionCalendar] = {"polygon": POLYGON_XAUUSD_CALENDAR}


def calendar_for(provider: str) -> SessionCalendar:
    """Select the validated market calendar for a data provider. Fail-closed: an unknown
    provider (e.g. 'xtb' before its calendar is empirically validated) raises rather than
    misapplying another provider's session boundaries."""
    cal = _CALENDARS.get(provider)
    if cal is None:
        raise ValueError(
            f"no validated market calendar for provider {provider!r} "
            "(XTB calendar pending derivation from live gaps)"
        )
    return cal


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
