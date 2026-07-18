"""Reconcile a shadow VirtualTrade against the bars that followed it.

- Intrabar SL/TP: a bar's high/low is used to detect a stop or target touch.
- BOTH-hit ambiguity: when a single bar's range contains BOTH the stop and the target we
  cannot know which filled first from OHLC alone. We DO NOT drop the case — we report a
  PESSIMISTIC (stop-first) and an OPTIMISTIC (target-first) R and flag it ambiguous. The
  primary R is the pessimistic one (conservative).
- R-multiple is NET of one round-trip spread cost (see virtual_broker). 1R = the entry->stop
  distance.
- Works with any bar timeframe passed in (M15 today; M1 later for finer intrabar resolution).
  Pass only bars AT/AFTER the entry, oldest-first.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from core.models import Direction
from data_collector.providers.base import Candle, floor_to_grid, timeframe_minutes
from shadow.virtual_broker import ShadowConfig, VirtualTrade, swap_rate_for


class Outcome(BaseModel):
    status: str                          # "closed" | "expired" | "open"
    exit_reason: str | None = None       # "tp_hit" | "sl_hit" | "ambiguous" | "timeout"
    exit_price: float | None = None
    closed_at: datetime | None = None
    r_multiple: float | None = None      # net of spread; pessimistic when ambiguous
    r_pessimistic: float | None = None
    r_optimistic: float | None = None
    ambiguous: bool = False


def _rollovers(opened_at: datetime, closed_at: datetime, rollover_hour: int,
               tz: str = "UTC", triple_weekday: int | None = None) -> float:
    """WEIGHTED count of daily-rollover boundaries strictly between the two instants — how many
    overnight swaps the position is charged.

    - DST-aware: a boundary is `rollover_hour` o'clock in `tz` (default UTC == literal 22:00 UTC,
      the legacy behaviour). With a real IANA tz the wall-clock hour is fixed and the UTC instant
      shifts across DST, so we iterate one LOCAL calendar day at a time.
    - Weekend: gold trades ~Sun->Fri, so no swap is charged on a Saturday or Sunday rollover — the
      weekend carry is folded into the triple-swap day (double-counting Sat AND Sun was a bug).
    - Triple-swap: a boundary whose local date's weekday equals `triple_weekday` (0=Mon..6=Sun)
      counts 3x — the standard weekend value-date roll (typically Wednesday). Off when None.
    """
    if closed_at <= opened_at:
        return 0.0
    zone = ZoneInfo(tz)
    open_local = opened_at.astimezone(zone)
    close_local = closed_at.astimezone(zone)

    def boundary_on(d) -> datetime:
        return datetime(d.year, d.month, d.day, rollover_hour, tzinfo=zone)

    day = open_local.date()
    b = boundary_on(day)
    if b <= open_local:                      # today's rollover already passed at open
        day += timedelta(days=1)
        b = boundary_on(day)
    total = 0.0
    while b < close_local:
        wd = b.weekday()
        if wd >= 5:                          # Sat (5) / Sun (6): market closed, no swap charged
            weight = 0.0
        elif triple_weekday is not None and wd == triple_weekday:
            weight = 3.0
        else:
            weight = 1.0
        total += weight
        day += timedelta(days=1)
        b = boundary_on(day)
    return total


def _extra_cost(trade: VirtualTrade, config, closed_at: datetime) -> float:
    """Commission (round-trip) + overnight swap (direction-aware, per weighted rollover), in price
    units. A long pays swapLong, a short pays swapShort; the triple-swap day counts 3x."""
    commission = trade.entry_mid * config.commission_pct / 100.0
    nights = _rollovers(trade.opened_at, closed_at, config.rollover_hour_utc,
                        tz=config.rollover_tz, triple_weekday=config.triple_swap_weekday)
    swap = nights * trade.entry_mid * swap_rate_for(config, trade.direction) / 100.0
    return commission + swap


def _r_net(trade: VirtualTrade, exit_fill: float, extra_cost: float = 0.0) -> float:
    """Signed R-multiple from the actual exit FILL, net of the round-trip spread cost plus any
    commission/swap (slippage is already baked into entry_mid and exit_fill)."""
    sign = 1.0 if trade.direction == Direction.BUY else -1.0
    gross = sign * (exit_fill - trade.entry_mid)
    spread_cost = trade.entry_mid * trade.spread_pct / 100.0  # one full spread, round trip
    risk = trade.risk_per_unit
    if risk <= 0:
        return 0.0
    return round((gross - spread_cost - extra_cost) / risk, 3)


def _exit_fill(trade: VirtualTrade, exit_ref: float) -> float:
    """Apply adverse exit slippage: closing a long SELLS (slip down), a short BUYS (slip up)."""
    s = trade.slippage_pct / 100.0
    return exit_ref * (1 - s) if trade.direction == Direction.BUY else exit_ref * (1 + s)


def _stop_exit_ref(trade: VirtualTrade, bar: Candle) -> float:
    """Gap-through-stop: if the bar OPENED already past the stop, the fill is at the open
    (worse), not idealised at the stop level."""
    if trade.direction == Direction.BUY:
        return min(trade.sl_price, bar.open)   # long stop is below; a gap-down fills lower
    return max(trade.sl_price, bar.open)       # short stop is above; a gap-up fills higher


def _hits(trade: VirtualTrade, bar: Candle) -> tuple[bool, bool]:
    """(stop_touched, target_touched) for this bar."""
    if trade.direction == Direction.BUY:
        return (bar.low <= trade.sl_price, bar.high >= trade.tp_price)
    return (bar.high >= trade.sl_price, bar.low <= trade.tp_price)


def _post_entry_bars(trade: VirtualTrade, bars: list[Candle]) -> list[Candle]:
    """Anti look-ahead: only bars that extend PAST the entry may resolve the trade (close_time >
    opened_at). This keeps the fully-post-entry bars AND the single PARTIAL bar the entry landed
    inside (open_time < opened_at < close_time) — the caller handles that one conservatively. The
    decision bar itself (close_time == opened_at) is excluded. Strictly increasing; no duplicates."""
    usable = [b for b in bars if b.close_time > trade.opened_at]
    prev = None
    for b in usable:
        if prev is not None and b.open_time <= prev.open_time:
            raise ValueError(
                f"reconcile bars not strictly increasing (duplicate/disorder) at {b.open_time.isoformat()}"
            )
        prev = b
    return usable


def covers_window(bars: list[Candle], opened_at: datetime, now: datetime, timeframe: str,
                  calendar) -> bool:
    """CALENDAR-AWARE coverage: True iff every market-OPEN bar between the first fully-post-entry
    boundary and `now` is present. A gap is allowed ONLY across market-closed time (weekend, daily
    session break, holiday) per the provider/instrument calendar — so a valid GOLD series crossing
    a weekend is NOT a coverage hole. False means an open-market bar is missing (e.g. the trade is
    older than the fetched window): a touch could hide there, so the trade must not be reconciled."""
    step = timedelta(minutes=timeframe_minutes(timeframe))
    have = {b.open_time for b in bars if b.close_time > opened_at}
    # Start at the bar CONTAINING opened_at (the partial entry bar): an SL/TP can be hit between
    # opened_at and that bar's close, so if it is missing while the market was open the window is
    # NOT covered. (Starting one bar later silently accepted a missing entry bar.)
    t = floor_to_grid(opened_at, timeframe)
    while t + step <= now:                            # only bars that have fully closed are expected
        if calendar.is_open(t) and t not in have:
            return False
        t += step
    return True


def select_reconcile_bars_for_trade(fine: list[Candle], coarse: list[Candle], *, opened_at: datetime,
                                    now: datetime, want_tf: str, trigger_tf: str,
                                    calendar) -> tuple[list[Candle], str, bool, bool]:
    """Choose the bars to reconcile ONE trade against. Prefer the finer bars when they calendar-
    cover the trade's window from entry; else fall back to the trigger timeframe. Returns
    (bars, timeframe_used, fell_back, covered). When `covered` is False NEITHER timeframe covers
    the window (fail-closed: the caller must not reconcile/mutate the trade)."""
    if want_tf != trigger_tf and fine and covers_window(fine, opened_at, now, want_tf, calendar):
        return fine, want_tf, False, True
    covered = covers_window(coarse, opened_at, now, trigger_tf, calendar)
    return coarse, trigger_tf, (want_tf != trigger_tf), covered


def reconcile(trade: VirtualTrade, bars: list[Candle], config: ShadowConfig | None = None) -> Outcome:
    config = config or ShadowConfig()
    # `timeout_bars` is a horizon in the TRIGGER timeframe (a fixed DURATION). Convert it to a
    # threshold in the CURRENT bar stream so the hold horizon is invariant to reconcile granularity
    # — otherwise 96 bars is ~24h on M15 but only 96 minutes on M1.
    timeout_threshold = config.timeout_bars * (
        timeframe_minutes(config.trigger_timeframe) / timeframe_minutes(config.reconcile_timeframe))
    for i, bar in enumerate(_post_entry_bars(trade, bars)):
        sl_hit, tp_hit = _hits(trade, bar)
        extra = _extra_cost(trade, config, bar.close_time)  # commission + swap for holding to here

        # PARTIAL entry bar (entry landed mid-bar): its OHLC mixes pre-/post-entry movement. Be
        # conservative — a stop touch still closes (pessimistic), but a TP-only touch is NOT
        # credited (deferred to a fully-post-entry bar). Fully-post-entry bars fall through.
        if config.conservative_partial_entry and bar.open_time < trade.opened_at:
            if sl_hit:
                # NOT _stop_exit_ref here: that models a gap THROUGH the stop using bar.open, and
                # this bar opened BEFORE we entered. Using it could fill at a price that existed
                # before the trade did — an impossible fill. We only know the stop was touched
                # somewhere in the bar, so the honest worst case we can justify is the stop level
                # itself plus adverse slippage.
                fill = _exit_fill(trade, trade.sl_price)
                r = _r_net(trade, fill, extra)
                return Outcome(status="closed", exit_reason="sl_hit", exit_price=round(fill, 4),
                               closed_at=bar.close_time, r_multiple=r, r_pessimistic=r, r_optimistic=r)
            if i + 1 >= timeout_threshold:
                fill = _exit_fill(trade, bar.close)
                r = _r_net(trade, fill, extra)
                return Outcome(status="expired", exit_reason="timeout", exit_price=round(fill, 4),
                               closed_at=bar.close_time, r_multiple=r, r_pessimistic=r, r_optimistic=r)
            continue   # no stop on the partial bar -> hold; do not credit a partial-bar TP

        if sl_hit and tp_hit:
            sl_fill = _exit_fill(trade, _stop_exit_ref(trade, bar))   # gap-through + slippage
            tp_fill = _exit_fill(trade, trade.tp_price)               # slippage
            r_pess, r_opt = _r_net(trade, sl_fill, extra), _r_net(trade, tp_fill, extra)
            return Outcome(
                status="closed", exit_reason="ambiguous", exit_price=round(sl_fill, 4),
                closed_at=bar.close_time, r_multiple=r_pess, r_pessimistic=r_pess,
                r_optimistic=r_opt, ambiguous=True,
            )
        if tp_hit:
            fill = _exit_fill(trade, trade.tp_price)
            r = _r_net(trade, fill, extra)
            return Outcome(status="closed", exit_reason="tp_hit", exit_price=round(fill, 4),
                           closed_at=bar.close_time, r_multiple=r, r_pessimistic=r, r_optimistic=r)
        if sl_hit:
            fill = _exit_fill(trade, _stop_exit_ref(trade, bar))
            r = _r_net(trade, fill, extra)
            return Outcome(status="closed", exit_reason="sl_hit", exit_price=round(fill, 4),
                           closed_at=bar.close_time, r_multiple=r, r_pessimistic=r, r_optimistic=r)

        if i + 1 >= timeout_threshold:  # no touch within the horizon -> time-based exit
            fill = _exit_fill(trade, bar.close)
            r = _r_net(trade, fill, extra)
            return Outcome(status="expired", exit_reason="timeout", exit_price=round(fill, 4),
                           closed_at=bar.close_time, r_multiple=r, r_pessimistic=r, r_optimistic=r)

    # ran out of bars without a touch and before the timeout -> still open
    return Outcome(status="open")
