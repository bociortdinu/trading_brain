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

from datetime import datetime

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


def _r_net(trade: VirtualTrade, exit_mid: float) -> float:
    """Signed R-multiple net of ONE round-trip spread cost."""
    sign = 1.0 if trade.direction == Direction.BUY else -1.0
    gross = sign * (exit_mid - trade.entry_mid)
    spread_cost = trade.entry_mid * trade.spread_pct / 100.0  # one full spread, round trip
    risk = trade.risk_per_unit
    if risk <= 0:
        return 0.0
    return round((gross - spread_cost) / risk, 3)


def _hits(trade: VirtualTrade, bar: Candle) -> tuple[bool, bool]:
    """(stop_touched, target_touched) for this bar."""
    if trade.direction == Direction.BUY:
        return (bar.low <= trade.sl_price, bar.high >= trade.tp_price)
    return (bar.high >= trade.sl_price, bar.low <= trade.tp_price)


def _post_entry_bars(trade: VirtualTrade, bars: list[Candle]) -> list[Candle]:
    """Anti look-ahead: only bars that OPEN at/after the entry may resolve the trade. The
    first eligible bar is the one whose open_time == opened_at (the bar that opens exactly at
    the decision bar's close). Enforces strict time ordering and rejects duplicates."""
    usable = [b for b in bars if b.open_time >= trade.opened_at]
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

        if sl_hit and tp_hit:
            r_pess = _r_net(trade, trade.sl_price)
            r_opt = _r_net(trade, trade.tp_price)
            return Outcome(
                status="closed", exit_reason="ambiguous", exit_price=trade.sl_price,
                closed_at=bar.close_time, r_multiple=r_pess, r_pessimistic=r_pess,
                r_optimistic=r_opt, ambiguous=True,
            )
        if tp_hit:
            r = _r_net(trade, trade.tp_price)
            return Outcome(status="closed", exit_reason="tp_hit", exit_price=trade.tp_price,
                           closed_at=bar.close_time, r_multiple=r, r_pessimistic=r, r_optimistic=r)
        if sl_hit:
            r = _r_net(trade, trade.sl_price)
            return Outcome(status="closed", exit_reason="sl_hit", exit_price=trade.sl_price,
                           closed_at=bar.close_time, r_multiple=r, r_pessimistic=r, r_optimistic=r)

        if i + 1 >= config.timeout_bars:  # no touch within the horizon -> time-based exit
            r = _r_net(trade, bar.close)
            return Outcome(status="expired", exit_reason="timeout", exit_price=round(bar.close, 4),
                           closed_at=bar.close_time, r_multiple=r, r_pessimistic=r, r_optimistic=r)

    # ran out of bars without a touch and before the timeout -> still open
    return Outcome(status="open")
