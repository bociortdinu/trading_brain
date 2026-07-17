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

from pydantic import BaseModel

from core.models import Direction
from data_collector.providers.base import Candle
from shadow.virtual_broker import ShadowConfig, VirtualTrade


class Outcome(BaseModel):
    status: str                          # "closed" | "expired" | "open"
    exit_reason: str | None = None       # "tp_hit" | "sl_hit" | "ambiguous" | "timeout"
    exit_price: float | None = None
    closed_at: datetime | None = None
    r_multiple: float | None = None      # net of spread; pessimistic when ambiguous
    r_pessimistic: float | None = None
    r_optimistic: float | None = None
    ambiguous: bool = False


def _rollovers(opened_at: datetime, closed_at: datetime, rollover_hour: int) -> int:
    """Number of daily-rollover boundaries (rollover_hour UTC) strictly between the two
    instants — i.e. how many nights the position is held over."""
    if closed_at <= opened_at:
        return 0
    boundary = opened_at.replace(hour=rollover_hour, minute=0, second=0, microsecond=0)
    if boundary <= opened_at:
        boundary += timedelta(days=1)
    nights = 0
    while boundary < closed_at:
        nights += 1
        boundary += timedelta(days=1)
    return nights


def _extra_cost(trade: VirtualTrade, config, closed_at: datetime) -> float:
    """Commission (round-trip) + overnight swap (per rollover held), in price units."""
    commission = trade.entry_mid * config.commission_pct / 100.0
    nights = _rollovers(trade.opened_at, closed_at, config.rollover_hour_utc)
    swap = nights * trade.entry_mid * config.swap_pct_per_night / 100.0
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


def reconcile(trade: VirtualTrade, bars: list[Candle], config: ShadowConfig | None = None) -> Outcome:
    config = config or ShadowConfig()
    for i, bar in enumerate(_post_entry_bars(trade, bars)):
        sl_hit, tp_hit = _hits(trade, bar)
        extra = _extra_cost(trade, config, bar.close_time)  # commission + swap for holding to here

        # PARTIAL entry bar (entry landed mid-bar): its OHLC mixes pre-/post-entry movement. Be
        # conservative — a stop touch still closes (pessimistic), but a TP-only touch is NOT
        # credited (deferred to a fully-post-entry bar). Fully-post-entry bars fall through.
        if config.conservative_partial_entry and bar.open_time < trade.opened_at:
            if sl_hit:
                fill = _exit_fill(trade, _stop_exit_ref(trade, bar))
                r = _r_net(trade, fill, extra)
                return Outcome(status="closed", exit_reason="sl_hit", exit_price=round(fill, 4),
                               closed_at=bar.close_time, r_multiple=r, r_pessimistic=r, r_optimistic=r)
            if i + 1 >= config.timeout_bars:
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

        if i + 1 >= config.timeout_bars:  # no touch within the horizon -> time-based exit
            fill = _exit_fill(trade, bar.close)
            r = _r_net(trade, fill, extra)
            return Outcome(status="expired", exit_reason="timeout", exit_price=round(fill, 4),
                           closed_at=bar.close_time, r_multiple=r, r_pessimistic=r, r_optimistic=r)

    # ran out of bars without a touch and before the timeout -> still open
    return Outcome(status="open")
