"""Scheduled-release calendar: parsing, DST-correct release times, the blackout gate, and the
guarantee that only FUTURE events ever reach the decision payload."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import httpx
import pytest

from data_collector.news.economic_calendar import (
    CalendarConfig,
    EconomicCalendar,
    FredCalendarError,
    FredCalendarProvider,
    ScheduledEvent,
    classify_release,
    parse_release_dates,
)
from tests.helpers import run


def _payload(rows):
    return {"release_dates": rows}


def _ev(label, when, impact="high"):
    return ScheduledEvent(label=label, release_name=label, scheduled_at=when, impact=impact)


# ---- release classification ---- #
def test_classifies_gold_relevant_releases():
    assert classify_release("Consumer Price Index").label == "CPI"
    assert classify_release("Employment Situation").label == "NFP"
    assert classify_release("Personal Income and Outlays").label == "PCE"


def test_matching_is_exact_not_substring():
    """Substring matching classified "Research Consumer Price Index" as CPI and "Debt to Gross
    Domestic Product Ratios" as GDP — neither is the market-moving release it was mistaken for."""
    assert classify_release("Research Consumer Price Index") is None
    assert classify_release("Debt to Gross Domestic Product Ratios") is None
    assert classify_release("consumer price index") is None      # exact, so case matters


def test_unknown_release_is_not_classified():
    """Hundreds of FRED releases don't move gold. An unmatched release must be dropped, not
    defaulted into something that triggers a blackout."""
    assert classify_release("Wheat Outlook") is None
    assert classify_release("") is None


# ---- parsing ---- #
def test_parse_keeps_only_relevant_releases():
    events = parse_release_dates(_payload([
        {"release_id": 10, "release_name": "Consumer Price Index", "date": "2026-08-12"},
        {"release_id": 99, "release_name": "Wheat Outlook", "date": "2026-08-12"},
    ]))
    assert [e.label for e in events] == ["CPI"]


def test_parse_rejects_a_payload_without_release_dates():
    with pytest.raises(FredCalendarError):
        parse_release_dates({"unexpected": []})


def test_parse_raises_on_an_unparseable_date():
    with pytest.raises(FredCalendarError):
        parse_release_dates(_payload([
            {"release_name": "Consumer Price Index", "date": "not-a-date"}]))


def test_parse_skips_rows_missing_fields():
    events = parse_release_dates(_payload([
        {"release_name": "Consumer Price Index"},        # no date
        {"date": "2026-08-12"},                          # no name
        {"release_name": "Consumer Price Index", "date": "2026-08-12"},
    ]))
    assert len(events) == 1


def test_parse_honours_the_requested_window():
    rows = [{"release_name": "Consumer Price Index", "date": d}
            for d in ("2026-07-15", "2026-08-12", "2026-09-10")]
    events = parse_release_dates(_payload(rows), start=date(2026, 8, 1), end=date(2026, 8, 31))
    assert [e.scheduled_at.date().isoformat() for e in events] == ["2026-08-12"]


# ---- DST ---- #
def test_release_time_is_dst_correct():
    """08:30 New York is 12:30 UTC in winter and 13:30 UTC in summer. A fixed offset would put
    every blackout window an hour off for half the year."""
    summer = parse_release_dates(_payload([
        {"release_name": "Consumer Price Index", "date": "2026-08-12"}]))[0]
    winter = parse_release_dates(_payload([
        {"release_name": "Consumer Price Index", "date": "2026-01-13"}]))[0]
    assert summer.scheduled_at == datetime(2026, 8, 12, 12, 30, tzinfo=timezone.utc)
    assert winter.scheduled_at == datetime(2026, 1, 13, 13, 30, tzinfo=timezone.utc)


def test_fomc_comes_from_the_curated_list_not_fred():
    """FRED's "FOMC Press Release" reports data-series updates, not meetings: 20 dates in one
    quarter, including consecutive days, which produced a daily 18:00 UTC blackout."""
    from data_collector.news.economic_calendar import fomc_events

    assert parse_release_dates(_payload([
        {"release_name": "FOMC Press Release", "date": "2026-08-25"}])) == []

    evs = fomc_events(date(2026, 7, 1), date(2026, 9, 30))
    assert [e.scheduled_at for e in evs] == [
        datetime(2026, 7, 29, 18, 0, tzinfo=timezone.utc),      # 14:00 ET
        datetime(2026, 9, 16, 18, 0, tzinfo=timezone.utc)]
    assert all(e.impact == "high" for e in evs)


def test_a_flooding_release_is_ignored():
    """Defensive: a FRED series that reports many dates per quarter is not an announcement
    schedule, and treating it as one would blanket the window in false blackouts."""
    rows = [{"release_name": "Consumer Price Index", "date": f"2026-07-{d:02d}"}
            for d in range(1, 21)]
    assert parse_release_dates(_payload(rows), start=date(2026, 7, 1),
                               end=date(2026, 9, 30)) == []


def test_fomc_coverage_is_reported_so_staleness_is_detectable():
    from data_collector.news.economic_calendar import fomc_coverage_ends

    assert fomc_coverage_ends() >= date(2026, 12, 1)


# ---- blackout ---- #
CPI = datetime(2026, 8, 12, 12, 30, tzinfo=timezone.utc)
CFG = CalendarConfig(blackout_minutes_before=30, blackout_minutes_after=15)


@pytest.mark.parametrize("offset_min,blacked", [
    (-45, False),   # before the window opens
    (-30, True),    # exactly at the opening edge
    (-1, True),
    (0, True),      # the release itself
    (15, True),     # exactly at the closing edge
    (16, False),    # after it closes
])
def test_blackout_window_boundaries(offset_min, blacked):
    cal = EconomicCalendar([_ev("CPI", CPI)])
    hit = cal.blackout(CPI + timedelta(minutes=offset_min), CFG)
    assert (hit is not None) is blacked


def test_default_after_window_frees_the_bar_after_the_release():
    """Regression on a MEASURED decision, not a preference. Against the real 2026-07-14 CPI
    (12:30 UTC) a 15-minute after-window blocked the 12:45 M15 bar — the only profitable trade
    in the sample. The default must block the release bar itself (bad fills) and free the next.
    """
    cal = EconomicCalendar([_ev("CPI", CPI)])
    default = CalendarConfig()
    assert cal.blackout(CPI, default) is not None                          # release bar: blocked
    assert cal.blackout(CPI + timedelta(minutes=15), default) is None      # next bar: free


def test_only_configured_impacts_trigger_a_blackout():
    cal = EconomicCalendar([_ev("PPI", CPI, impact="medium")])
    assert cal.blackout(CPI, CFG) is None
    wide = CalendarConfig(blackout_impacts=("high", "medium"))
    assert cal.blackout(CPI, wide) is not None


def test_blackout_names_the_event_for_the_audit_trail():
    cal = EconomicCalendar([_ev("CPI", CPI)])
    assert cal.blackout(CPI, CFG).label == "CPI"


def test_empty_calendar_never_blacks_out():
    assert EconomicCalendar([]).blackout(CPI, CFG) is None


# ---- context: future events only ---- #
def test_upcoming_excludes_past_events():
    """The decision payload must never hint at a release that already fired — we carry no
    outcomes, and a past event invites reasoning about a number the model cannot see."""
    cal = EconomicCalendar([_ev("CPI", CPI), _ev("NFP", CPI + timedelta(hours=2))])
    upcoming = cal.upcoming(CPI + timedelta(minutes=1), CalendarConfig())
    assert [e.label for e in upcoming] == ["NFP"]


def test_upcoming_respects_the_lookahead_horizon():
    cal = EconomicCalendar([_ev("NFP", CPI + timedelta(hours=10))])
    assert cal.upcoming(CPI, CalendarConfig(context_lookahead_minutes=60)) == []


def test_upcoming_is_capped():
    cal = EconomicCalendar([_ev(f"E{i}", CPI + timedelta(minutes=10 * (i + 1))) for i in range(6)])
    assert len(cal.upcoming(CPI, CalendarConfig(max_context_events=2))) == 2


def test_digest_is_relative_minutes():
    ev = _ev("CPI", CPI)
    assert ev.to_digest(CPI - timedelta(minutes=34)) == {
        "event": "CPI", "impact": "high", "in_minutes": 34}


# ---- provider ---- #
def test_provider_requires_an_api_key():
    with pytest.raises(FredCalendarError):
        FredCalendarProvider("")


def test_provider_fetches_and_builds_a_calendar():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/fred/releases/dates"
        assert request.url.params["file_type"] == "json"
        assert request.url.params["include_release_dates_with_no_data"] == "true"
        return httpx.Response(200, json=_payload([
            {"release_name": "Consumer Price Index", "date": "2026-08-12"},
            {"release_name": "Wheat Outlook", "date": "2026-08-12"},
        ]))

    p = FredCalendarProvider("k" * 32, transport=httpx.MockTransport(handler))
    cal = run(p.fetch(date(2026, 8, 1), date(2026, 8, 31)))
    run(p.aclose())
    assert len(cal) == 1 and cal.events[0].label == "CPI"


def test_provider_raises_on_a_fred_error_payload():
    p = FredCalendarProvider("k" * 32, transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"error_code": 400, "error_message": "bad key"})))
    with pytest.raises(FredCalendarError, match="bad key"):
        run(p.fetch(date(2026, 8, 1), date(2026, 8, 31)))
    run(p.aclose())


def test_provider_raises_on_http_error():
    p = FredCalendarProvider("k" * 32, transport=httpx.MockTransport(
        lambda r: httpx.Response(500)))
    with pytest.raises(FredCalendarError):
        p_fetch = p.fetch(date(2026, 8, 1), date(2026, 8, 31))
        run(p_fetch)
    run(p.aclose())
