"""Scheduled macro releases (economic calendar) — the gold-relevant half of "news".

Why this is NOT the news problem
--------------------------------
`docs/NEWS_PROVIDER_REQUIREMENTS.md` demands a bitemporal feed because a *story* is only
knowable after it is published, and corrections rewrite the past. A **calendar** is a
different object: the schedule ("CPI is released 2026-08-12 at 13:30 UTC") is published
weeks to years in advance, so it was knowable at any decision time we replay. That makes it
replay-safe by construction — provided we use ONLY the schedule.

So this module deliberately carries the *event*, never the *outcome*. It never records what
CPI printed, only that a CPI print is due at T. Reading the released VALUE at a bar before
its publication would be look-ahead; there is no value here to leak.

What it is for
--------------
1. **Blackout gate (deterministic, free).** Gold reprices violently on CPI/NFP/FOMC. An M15
   stop of ~0.28% is meaningless across such a print. Bars inside the blackout window never
   reach the model — that prevents the loss AND costs nothing to evaluate.
2. **Context for the decision.** "Next high-impact release in 34 minutes" is exactly the kind
   of judgement input the model should weigh.

FRED gives release DATES, not times. US macro release times are fixed by the publishing
agency (BLS/BEA/Census at 08:30 ET, FOMC at 14:00 ET), so the time-of-day comes from
`_RELEASE_SPECS` and is converted from America/New_York — DST-aware, since the same release
is 12:30 UTC in winter and 13:30 UTC in summer.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel, Field

_NY = ZoneInfo("America/New_York")

Impact = Literal["low", "medium", "high"]


class ReleaseSpec(BaseModel):
    """How a FRED release maps onto a tradeable event.

    `release_name` is matched EXACTLY. Substring matching was tried and is wrong: "Research
    Consumer Price Index" is not CPI, and "Debt to Gross Domestic Product Ratios" is not GDP —
    both would have been classified as market-moving releases they have nothing to do with.
    """
    release_name: str
    label: str
    local_time: time
    impact: Impact


# Curated, gold-relevant releases. Gold is driven by real yields, the dollar and Fed policy
# expectations, so the inflation and labour prints that move them dominate.
#
# FOMC IS DELIBERATELY ABSENT. FRED's "FOMC Press Release" is not a meeting calendar — it
# reports when that data series was updated, which was 20 separate dates in one quarter,
# including consecutive days. Treating those as events produced a daily 18:00 UTC blackout.
# Real FOMC meeting dates come from _FOMC_MEETINGS below instead.
_RELEASE_SPECS: tuple[ReleaseSpec, ...] = (
    ReleaseSpec(release_name="Consumer Price Index", label="CPI",
                local_time=time(8, 30), impact="high"),
    ReleaseSpec(release_name="Employment Situation", label="NFP",
                local_time=time(8, 30), impact="high"),
    ReleaseSpec(release_name="Personal Income and Outlays", label="PCE",
                local_time=time(8, 30), impact="high"),
    ReleaseSpec(release_name="Producer Price Index", label="PPI",
                local_time=time(8, 30), impact="medium"),
    ReleaseSpec(release_name="Advance Monthly Sales for Retail and Food Services",
                label="RetailSales", local_time=time(8, 30), impact="medium"),
    ReleaseSpec(release_name="Gross Domestic Product", label="GDP",
                local_time=time(8, 30), impact="medium"),
    ReleaseSpec(release_name="Job Openings and Labor Turnover Survey", label="JOLTS",
                local_time=time(10, 0), impact="medium"),
)

# FOMC decision days, 14:00 ET. The Fed publishes these years ahead, so a short curated list is
# both accurate and replay-safe — but it EXPIRES. `fomc_coverage_ends` lets callers detect that
# the list has run out rather than silently losing the single most important event for gold.
_FOMC_MEETINGS: tuple[date, ...] = (
    date(2026, 1, 28), date(2026, 3, 18), date(2026, 4, 29), date(2026, 6, 17),
    date(2026, 7, 29), date(2026, 9, 16), date(2026, 11, 4), date(2026, 12, 16),
)

# A single release should fire a handful of times per quarter. Far more means the FRED series is
# reporting data-update dates rather than announcements (as FOMC does), and treating those as
# events would blanket the calendar in false blackouts.
_MAX_DATES_PER_RELEASE_PER_QUARTER = 6


def fomc_coverage_ends() -> date:
    """Last FOMC date we know about. Past this, the calendar is silently missing FOMC."""
    return max(_FOMC_MEETINGS)


def classify_release(release_name: str) -> ReleaseSpec | None:
    """The spec for a FRED release name, or None when it is not a release we react to.
    EXACT match — see ReleaseSpec for why substring matching is wrong."""
    for spec in _RELEASE_SPECS:
        if spec.release_name == release_name:
            return spec
    return None


class ScheduledEvent(BaseModel):
    """One scheduled macro release. `scheduled_at` is UTC and DST-correct."""
    label: str
    release_name: str
    scheduled_at: datetime
    impact: Impact

    def to_digest(self, as_of: datetime) -> dict:
        """Compact form for the decision payload: minutes are what the model can reason about,
        absolute timestamps just burn tokens."""
        delta_min = round((self.scheduled_at - as_of).total_seconds() / 60)
        return {"event": self.label, "impact": self.impact, "in_minutes": delta_min}


def _to_utc(day: date, local: time) -> datetime:
    """Attach the agency's local publication time and convert to UTC. DST-aware: an 08:30 ET
    release is 13:30 UTC in summer and 12:30 UTC in winter — a fixed offset would put the
    blackout window an hour off for half the year."""
    return datetime.combine(day, local, tzinfo=_NY).astimezone(timezone.utc)


class CalendarConfig(BaseModel):
    """Blackout geometry.

    `before` is generous: entering minutes ahead of a print is a coin flip on the outcome, and
    an M15 stop can be gapped straight through.

    `after` is deliberately SHORT, and this was measured, not assumed. Backtested against the
    real 2026-07-14 CPI (12:30 UTC), a 15-minute after-window blocked the 12:45 bar — the only
    profitable trade in the sample (+1.94R), replacing it with a -1.06R entry one bar later.
    The rationale for blocking *before* a release (unknown outcome) does NOT carry over to
    *after* it: the direction is resolved and the post-release move is often the cleanest trade
    of the day. What remains worth avoiding is the release bar itself, where the spread blows
    out and fills are poor — 5 minutes covers exactly that and frees the bar after.
    """
    blackout_minutes_before: int = Field(30, ge=0)
    blackout_minutes_after: int = Field(5, ge=0)
    blackout_impacts: tuple[Impact, ...] = ("high",)
    context_lookahead_minutes: int = Field(240, ge=0)   # how far ahead the model is told about
    max_context_events: int = Field(3, ge=1)


class EconomicCalendar:
    """An immutable, already-fetched set of scheduled events, queried by `as_of`.

    Holding the schedule in memory (rather than calling out per bar) is what makes a backtest
    over thousands of bars practical: the schedule is static, so it is fetched once.
    """

    def __init__(self, events: list[ScheduledEvent]) -> None:
        self._events = sorted(events, key=lambda e: e.scheduled_at)

    def __len__(self) -> int:
        return len(self._events)

    @property
    def events(self) -> list[ScheduledEvent]:
        return list(self._events)

    def blackout(self, as_of: datetime, config: CalendarConfig) -> ScheduledEvent | None:
        """The event whose blackout window contains `as_of`, or None. Returns the event so the
        caller can name it in the skip reason — an unexplained skip is not auditable."""
        before = timedelta(minutes=config.blackout_minutes_before)
        after = timedelta(minutes=config.blackout_minutes_after)
        for ev in self._events:
            if ev.impact not in config.blackout_impacts:
                continue
            if ev.scheduled_at - before <= as_of <= ev.scheduled_at + after:
                return ev
        return None

    def upcoming(self, as_of: datetime, config: CalendarConfig) -> list[ScheduledEvent]:
        """Scheduled events strictly ahead of `as_of`, within the lookahead horizon.

        Only FUTURE events are exposed. A release that already happened would invite the model
        to reason about an outcome it cannot see, and we deliberately do not carry outcomes.
        """
        horizon = as_of + timedelta(minutes=config.context_lookahead_minutes)
        ahead = [e for e in self._events if as_of < e.scheduled_at <= horizon]
        return ahead[: config.max_context_events]


class FredCalendarError(RuntimeError):
    """Unrecoverable failure fetching or parsing the FRED release calendar."""


class FredCalendarProvider:
    """Fetches the release schedule from the FRED API (free key, St. Louis Fed).

    Endpoint: GET /fred/releases/dates -> {"release_dates": [{"release_id", "release_name",
    "date"}, ...]}. `release_name` is present only with
    `include_release_dates_with_no_data=true`, which we always send.
    """

    BASE_URL = "https://api.stlouisfed.org"

    def __init__(self, api_key: str, *, base_url: str | None = None,
                 timeout_seconds: float = 15.0,
                 transport: httpx.BaseTransport | None = None) -> None:
        if not api_key:
            raise FredCalendarError("a FRED API key is required (free: fredaccount.stlouisfed.org)")
        self._api_key = api_key
        self._client = httpx.AsyncClient(base_url=base_url or self.BASE_URL,
                                         timeout=timeout_seconds, transport=transport)

    async def __aenter__(self) -> "FredCalendarProvider":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def fetch(self, start: date, end: date, *, page_size: int = 1000,
                    max_pages: int = 10) -> EconomicCalendar:
        """Fetch every scheduled release between `start` and `end`, then add FOMC.

        Paginated because FRED caps `limit` at 1000 while a quarter can return ~2400 rows —
        a single request silently truncated the calendar.

        `include_release_dates_with_no_data=true` is REQUIRED and was verified: with it false FRED
        returns only releases whose data has already landed, i.e. the PAST. A future CPI has no
        data yet, so the flag that looks like noise reduction silently removes every event a
        blackout could ever act on (measured: false -> CPI 2026-07-14 only; true -> 07-14, 08-12,
        09-11, matching the BLS schedule).

        The cost of `true` is that some FRED series then report data-update dates rather than
        announcements — that is what `_MAX_DATES_PER_RELEASE_PER_QUARTER` and exact-name matching
        exist to absorb, and why FOMC comes from the curated list instead.
        """
        rows: list[dict] = []
        for page in range(max_pages):
            params = {
                "api_key": self._api_key,
                "file_type": "json",
                "realtime_start": start.isoformat(),
                "realtime_end": end.isoformat(),
                "include_release_dates_with_no_data": "true",
                "limit": page_size,
                "offset": page * page_size,
            }
            try:
                resp = await self._client.get("/fred/releases/dates", params=params)
            except httpx.HTTPError as exc:
                raise FredCalendarError(f"FRED request failed: {exc}") from exc
            if resp.status_code != 200:
                raise FredCalendarError(f"FRED returned HTTP {resp.status_code}")
            try:
                payload = resp.json()
            except ValueError as exc:
                raise FredCalendarError("FRED response is not JSON") from exc
            if "error_message" in payload:
                raise FredCalendarError(f"FRED error: {payload['error_message']}")
            batch = payload.get("release_dates")
            if not isinstance(batch, list):
                raise FredCalendarError("FRED payload has no 'release_dates' list")
            rows.extend(batch)
            if len(batch) < page_size:
                break

        events = parse_release_dates({"release_dates": rows}, start=start, end=end)
        events.extend(fomc_events(start, end))
        return EconomicCalendar(events)

async def build_calendar(settings, *, start: date, end: date):
    """Fetch the release schedule for a run, or return (None, reason) when unavailable.

    Returns `(EconomicCalendar | None, str | None)`. Fail-OPEN by design: a missing key or a
    FRED outage must not halt trading, and the caller reports news status as 'unavailable'
    rather than telling the model there are no events — those mean different things.
    """
    key = getattr(settings, "fred_api_key", None)
    if not key:
        return None, "no_fred_api_key"
    provider = FredCalendarProvider(
        key, timeout_seconds=getattr(settings, "http_timeout_seconds", 15.0))
    try:
        return await provider.fetch(start, end), None
    except FredCalendarError as exc:
        return None, f"calendar_unavailable:{type(exc).__name__}"
    finally:
        await provider.aclose()


def calendar_config_from_settings(settings) -> CalendarConfig:
    return CalendarConfig(
        blackout_minutes_before=getattr(settings, "calendar_blackout_minutes_before", 30),
        blackout_minutes_after=getattr(settings, "calendar_blackout_minutes_after", 5),
        context_lookahead_minutes=getattr(settings, "calendar_context_lookahead_minutes", 240),
    )


def fomc_events(start: date, end: date) -> list[ScheduledEvent]:
    """FOMC decision days in range, at 14:00 ET. Sourced from the curated list rather than FRED —
    see _RELEASE_SPECS for why FRED's "FOMC Press Release" dates are not meetings."""
    return [ScheduledEvent(label="FOMC", release_name="FOMC decision",
                           scheduled_at=_to_utc(d, time(14, 0)), impact="high")
            for d in _FOMC_MEETINGS if start <= d <= end]


def parse_release_dates(payload: dict, *, start: date | None = None,
                        end: date | None = None) -> list[ScheduledEvent]:
    """Map a FRED `/fred/releases/dates` payload to the events we react to.

    Unknown releases are dropped rather than defaulted to a blackout-triggering impact —
    FRED publishes hundreds of releases and most move gold not at all.
    """
    rows = payload.get("release_dates")
    if not isinstance(rows, list):
        raise FredCalendarError("FRED payload has no 'release_dates' list")

    # Guard against a FRED series that reports data-update dates rather than announcements:
    # such a release floods the window and would blanket it in false blackouts.
    from collections import Counter

    per_release = Counter(r.get("release_name") for r in rows if isinstance(r, dict))
    span_quarters = max(1.0, ((end - start).days / 91.0)) if (start and end) else 1.0
    noisy = {name for name, n in per_release.items()
             if name and n / span_quarters > _MAX_DATES_PER_RELEASE_PER_QUARTER}

    events: list[ScheduledEvent] = []
    for row in rows:
        name = row.get("release_name")
        raw_date = row.get("date")
        if not name or not raw_date:
            continue
        if name in noisy:
            continue          # not an announcement schedule — see the guard above
        spec = classify_release(name)
        if spec is None:
            continue
        try:
            day = date.fromisoformat(raw_date)
        except (TypeError, ValueError) as exc:
            raise FredCalendarError(f"unparseable release date {raw_date!r}") from exc
        if (start and day < start) or (end and day > end):
            continue
        events.append(ScheduledEvent(label=spec.label, release_name=name,
                                     scheduled_at=_to_utc(day, spec.local_time),
                                     impact=spec.impact))
    return events
