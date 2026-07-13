"""Decision eligibility: recent-window gaps + freshness + online quote (online vs replay).

Eligibility is a contextual verdict, separate from the snapshot. The same bar is
evaluated here under both modes to show the SAME observation can be eligible in replay
and ineligible online — the reason a single boolean on the snapshot was wrong.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from features.eligibility import EligibilityConfig, evaluate_eligibility
from tests.synthetic import trend

UTC = timezone.utc


def _cfg():
    return EligibilityConfig(
        recent_window_bars={"15min": 8, "1h": 6, "4h": 4, "1day": 3},
        max_feed_lag_seconds=1800, max_quote_lag_seconds=120,
    )


def _m15(end, n=250):
    return trend(step=1.0, tf_min=15, start=end - timedelta(minutes=15 * n))


def _fresh_quote(now):
    return now - timedelta(seconds=5)


def test_online_stale_feed_is_ineligible():
    end = datetime(2026, 7, 10, 20, 15, tzinfo=UTC)  # Friday feed
    now = datetime(2026, 7, 13, 14, 0, tzinfo=UTC)   # evaluated Monday
    r = evaluate_eligibility({"15min": _m15(end)}, "15min", end, mode="online",
                             now=now, config=_cfg(), quote_time=_fresh_quote(now))
    assert not r.eligible
    assert any("stale_feed" in x for x in r.reasons)
    assert r.mode == "online" and r.policy_version.startswith("elig-")


def test_replay_is_fresh_vs_as_of():
    end = datetime(2026, 7, 10, 20, 15, tzinfo=UTC)
    now = datetime(2026, 7, 13, 14, 0, tzinfo=UTC)   # Monday, but replay ignores wall clock
    r = evaluate_eligibility({"15min": _m15(end)}, "15min", end, mode="replay", now=now, config=_cfg())
    assert r.eligible and r.reasons == []
    assert r.ref_now is None            # replay never records a wall clock
    assert r.quote_present is None      # replay never requires a live quote


def test_same_bar_eligible_replay_but_stale_online():
    # ONE observation, TWO verdicts — this is why eligibility can't be a single boolean.
    end = datetime(2026, 7, 10, 20, 15, tzinfo=UTC)
    now = datetime(2026, 7, 13, 14, 0, tzinfo=UTC)
    candles = {"15min": _m15(end)}
    online = evaluate_eligibility(candles, "15min", end, mode="online", now=now,
                                  config=_cfg(), quote_time=_fresh_quote(now))
    replay = evaluate_eligibility(candles, "15min", end, mode="replay", now=now, config=_cfg())
    assert replay.eligible and not online.eligible
    assert online.policy_version == replay.policy_version  # same policy, different verdict


def test_online_missing_quote_fails_closed():
    end = datetime(2026, 7, 13, 14, 0, tzinfo=UTC)   # fresh feed
    r = evaluate_eligibility({"15min": _m15(end)}, "15min", end, mode="online",
                             now=end, config=_cfg(), quote_time=None)  # XTB quote absent
    assert not r.eligible
    assert "missing_xtb_quote" in r.reasons
    assert r.quote_present is False


def test_online_stale_quote_is_ineligible():
    end = datetime(2026, 7, 13, 14, 0, tzinfo=UTC)   # fresh feed
    now = end
    stale_quote = end - timedelta(minutes=10)        # quote 10 min old > 120s threshold
    r = evaluate_eligibility({"15min": _m15(end)}, "15min", end, mode="online",
                             now=now, config=_cfg(), quote_time=stale_quote)
    assert not r.eligible and any("stale_quote" in x for x in r.reasons)
    assert r.quote_present is True and r.quote_lag_seconds == 600.0


def test_online_future_quote_is_rejected():
    # A quote timestamped AFTER now (negative lag beyond skew) is fail-closed (look-ahead).
    end = datetime(2026, 7, 13, 14, 0, tzinfo=UTC)
    now = end
    future_quote = end + timedelta(seconds=30)   # 30s in the future
    r = evaluate_eligibility({"15min": _m15(end)}, "15min", end, mode="online",
                             now=now, config=_cfg(), quote_time=future_quote)
    assert not r.eligible and any("future_quote" in x for x in r.reasons)


def test_recent_unexpected_gap_blocks():
    end = datetime(2026, 7, 8, 16, 0, tzinfo=UTC)    # Wed 12:00 EDT, market open
    candles = _m15(end)
    del candles[-3]                                  # hole within the last 8 bars
    r = evaluate_eligibility({"15min": candles}, "15min", candles[-1].close_time,
                             mode="replay", now=end, config=_cfg())
    assert not r.eligible and any("recent_unexpected_gap" in x for x in r.reasons)


def test_old_gap_outside_window_is_non_blocking():
    end = datetime(2026, 7, 8, 16, 0, tzinfo=UTC)
    candles = _m15(end)
    del candles[50]                                  # hole far back, outside the recent window
    r = evaluate_eligibility({"15min": candles}, "15min", candles[-1].close_time,
                             mode="replay", now=end, config=_cfg())
    assert r.eligible and r.reasons == []


def test_policy_version_changes_with_config():
    base = _cfg()
    tighter = EligibilityConfig(
        recent_window_bars=base.recent_window_bars,
        max_feed_lag_seconds=600, max_quote_lag_seconds=120,
    )
    assert base.policy_version() != tighter.policy_version()  # threshold change -> new policy
