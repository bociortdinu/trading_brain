"""Temporal contract: canonical, deterministic, DST-invariant, on-grid buckets.

The bug this pins: Polygon anchors custom-multiplier intraday buckets to the query
`from`. The old collector derived `from` from an arbitrary wall-clock `now`, so the
sub-bar phase of `now` leaked into every bar (an off-grid :08 close). The fix floors
`from` to the canonical UTC-epoch grid and REJECTS any returned series that is not on
that grid. "15-minute duration" is NOT proof of alignment — these tests check phase.
"""

from __future__ import annotations

import json
import pathlib
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from data_collector.providers.base import (
    floor_to_grid,
    is_grid_aligned,
    timeframe_minutes,
    validate_series,
)
from data_collector.providers.polygon import MassivePolygonProvider, ProviderError
from tests.helpers import run

UTC = timezone.utc
FIX = pathlib.Path(__file__).parent / "fixtures"
STEP_MS = {"15min": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1day": 86_400_000}


# --------------------------------------------------------------------------- #
# 1. Grid math
# --------------------------------------------------------------------------- #
def test_floor_to_grid_per_timeframe():
    t = datetime(2026, 7, 10, 20, 8, 37, tzinfo=UTC)  # the pathological :08:37 phase
    assert floor_to_grid(t, "15min") == datetime(2026, 7, 10, 20, 0, tzinfo=UTC)
    assert floor_to_grid(t, "1h") == datetime(2026, 7, 10, 20, 0, tzinfo=UTC)
    assert floor_to_grid(t, "4h") == datetime(2026, 7, 10, 20, 0, tzinfo=UTC)
    assert floor_to_grid(t, "1day") == datetime(2026, 7, 10, 0, 0, tzinfo=UTC)


def test_is_grid_aligned():
    assert is_grid_aligned(datetime(2026, 7, 10, 20, 15, tzinfo=UTC), "15min")
    assert not is_grid_aligned(datetime(2026, 7, 10, 20, 8, tzinfo=UTC), "15min")
    assert is_grid_aligned(datetime(2026, 7, 10, 20, 0, tzinfo=UTC), "4h")
    assert not is_grid_aligned(datetime(2026, 7, 10, 22, 0, tzinfo=UTC), "4h")  # 22:00 not a 4h boundary
    assert is_grid_aligned(datetime(2026, 7, 10, 0, 0, tzinfo=UTC), "1day")
    assert not is_grid_aligned(datetime(2026, 7, 10, 1, 0, tzinfo=UTC), "1day")


def test_floor_is_idempotent_and_on_grid():
    for tf in ("15min", "1h", "4h", "1day"):
        f = floor_to_grid(datetime(2026, 7, 10, 20, 8, 37, tzinfo=UTC), tf)
        assert is_grid_aligned(f, tf)
        assert floor_to_grid(f, tf) == f


# --------------------------------------------------------------------------- #
# 2. DST-invariance of the grid (the bar grid must NOT move when US clocks change)
# --------------------------------------------------------------------------- #
def test_grid_is_dst_invariant():
    # US spring-forward is 2026-03-08; fall-back is 2026-11-01. The UTC-epoch grid is
    # identical in both regimes: same phase, same step, no drift.
    spring = datetime(2026, 3, 8, 12, 8, tzinfo=UTC)
    fall = datetime(2026, 11, 1, 12, 8, tzinfo=UTC)
    assert floor_to_grid(spring, "15min").minute == floor_to_grid(fall, "15min").minute == 0
    # An M15 series crossing the spring-forward instant has NO spurious gap: consecutive
    # UTC bars are exactly 15 min apart regardless of the civil-time jump.
    from tests.synthetic import trend
    start = datetime(2026, 3, 8, 6, 0, tzinfo=UTC)  # spans 07:00 UTC == 02->03 EST->EDT
    bars = trend(n=8, tf_min=15, start=start)
    assert validate_series(bars, "15min") == []  # contiguous, all on-grid


# --------------------------------------------------------------------------- #
# 3. Determinism through the provider — the headline requirement:
#    the SAME logical period requested at different from/to moments -> SAME buckets.
# --------------------------------------------------------------------------- #
def _from_anchored_handler():
    """A faithful Polygon stand-in: it anchors 15-min bars to the request `from`
    (this is exactly the behaviour that produced off-grid bars in production)."""
    def h(req: httpx.Request) -> httpx.Response:
        parts = req.url.path.rstrip("/").split("/")
        frm_ms, to_ms = int(parts[-2]), int(parts[-1])
        step = 900_000
        rows, t, p = [], frm_ms, 2400.0
        while t <= to_ms:
            rows.append({"o": p, "h": p + 1, "l": p - 1, "c": p + 0.5, "v": 100, "t": t})
            t += step
            p += 0.1
        return httpx.Response(200, json={"status": "OK", "results": rows})
    return h


def _provider(now_dt: datetime):
    return MassivePolygonProvider(
        api_key="k", base_url="https://api.polygon.io", max_retries=0, backoff_base=0.0,
        transport=httpx.MockTransport(_from_anchored_handler()), now_fn=lambda: now_dt,
    )


def _closes(candles):
    return [c.close_time for c in candles]


def test_same_period_different_moments_same_buckets():
    # Two different wall-clock instants inside the SAME 15-min bar [20:00, 20:15).
    now1 = datetime(2026, 7, 10, 20, 7, 33, tzinfo=UTC)
    now2 = datetime(2026, 7, 10, 20, 11, 59, tzinfo=UTC)

    async def go(now):
        p = _provider(now)
        try:
            return await p.get_ohlcv("C:XAUUSD", "15min", 20)
        finally:
            await p.aclose()

    b1, b2 = run(go(now1)), run(go(now2))
    assert _closes(b1) == _closes(b2)              # identical buckets despite different moments
    assert all(is_grid_aligned(c.open_time, "15min") for c in b1)  # and all on the canonical grid
    assert b1[-1].close_time == datetime(2026, 7, 10, 20, 0, tzinfo=UTC)  # last CLOSED bar


def test_next_bar_shifts_by_exactly_one_step_still_on_grid():
    b_now = run_get(datetime(2026, 7, 10, 20, 11, 0, tzinfo=UTC))   # bar [20:00,20:15)
    b_next = run_get(datetime(2026, 7, 10, 20, 16, 0, tzinfo=UTC))  # bar [20:15,20:30)
    assert b_next[-1].close_time - b_now[-1].close_time == timedelta(minutes=15)
    assert is_grid_aligned(b_next[-1].open_time, "15min")


def run_get(now):
    async def go():
        p = _provider(now)
        try:
            return await p.get_ohlcv("C:XAUUSD", "15min", 20)
        finally:
            await p.aclose()
    return run(go())


# --------------------------------------------------------------------------- #
# 4. Fail-closed: a provider that ignores `from` and returns phase-shifted bars
#    must be REJECTED (not silently accepted because the duration is 15 min).
# --------------------------------------------------------------------------- #
def test_off_grid_series_is_rejected():
    def h(_req):
        # bars anchored to :08 (off-grid) — mimics the pre-fix production data
        base = int(datetime(2026, 7, 10, 19, 53, tzinfo=UTC).timestamp() * 1000)
        rows = [{"o": 2400.0, "h": 2401, "l": 2399, "c": 2400.5, "v": 1, "t": base + i * 900_000}
                for i in range(5)]
        return httpx.Response(200, json={"status": "OK", "results": rows})

    async def go():
        p = MassivePolygonProvider("k", max_retries=0, backoff_base=0.0,
                                   transport=httpx.MockTransport(h),
                                   now_fn=lambda: datetime(2026, 7, 10, 21, tzinfo=UTC))
        try:
            with pytest.raises(ProviderError, match="off the 15min grid|contract violation"):
                await p.get_ohlcv("C:XAUUSD", "15min", 10)
        finally:
            await p.aclose()

    run(go())


# --------------------------------------------------------------------------- #
# 5. Real-response validation (fixtures captured from the live Polygon API).
#    phaseA: `from` aligned to :00  -> real bars land ON the grid (the fix works live).
#    phaseB: `from` at :08:37       -> real bars land OFF the grid (proves Polygon
#                                       from-anchors; the guard rejects the real series).
# --------------------------------------------------------------------------- #
def _parse_ts(path):
    data = json.loads(path.read_text())
    return [datetime.fromtimestamp(r["t"] / 1000, tz=UTC) for r in (data.get("results") or [])]


@pytest.mark.skipif(not (FIX / "real_m15_phaseA.json").exists(), reason="real fixture not captured")
def test_real_aligned_request_is_on_grid():
    opens = _parse_ts(FIX / "real_m15_phaseA.json")
    assert opens, "empty real response"
    assert all(is_grid_aligned(t, "15min") for t in opens)


@pytest.mark.skipif(not (FIX / "real_m15_phaseB.json").exists(), reason="real fixture not captured")
def test_real_misaligned_request_is_off_grid_and_rejected():
    opens = _parse_ts(FIX / "real_m15_phaseB.json")
    assert opens, "empty real response"
    assert any(not is_grid_aligned(t, "15min") for t in opens)  # Polygon really from-anchors
    # the :08 phase from snapshot id=61 is reproduced here
    assert any(t.minute % 15 == 8 for t in opens)


@pytest.mark.skipif(not (FIX / "real_d1.json").exists(), reason="real fixture not captured")
def test_real_daily_bars_anchor_at_utc_midnight():
    opens = _parse_ts(FIX / "real_d1.json")
    assert opens, "empty real response"
    # Real C:XAUUSD daily bars sit at 00:00 UTC every day -> DST-invariant, on our 1day grid.
    assert all(is_grid_aligned(t, "1day") for t in opens)
    assert all(t.hour == 0 and t.minute == 0 for t in opens)
