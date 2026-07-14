"""Shadow virtual broker: open a VIRTUAL position from a risk-approved decision.

Measurement only — NO real order is placed, and account balance/equity is NEVER used as PnL.

MODELLING HONESTY (this is an approximation, not a bid/ask microstructure simulation):
- Bars are treated as MID prices. SL/TP are absolute MID levels from the entry mid and the
  deterministic sl_pct / tp_pct the Risk Engine produced.
- The bid/ask spread is charged as ONE flat round-trip cost deducted from the R-multiple
  (see reconciler `_r_net`). Touch thresholds are NOT bid/ask-adjusted; a gap through the
  stop fills IDEALLY at the stop level; there is no latency or slippage yet. Those are
  refinements tracked for later in the Faza 3 plan.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from core.models import Direction

SpreadProvenance = Literal["observed_xtb", "modeled", "historical"]


class ShadowConfig(BaseModel):
    # Close the trade if neither SL nor TP is hit within this many bars (of whatever
    # timeframe is fed to the reconciler). ~1 trading day of M15 bars by default.
    timeout_bars: int = 96


class VirtualTrade(BaseModel):
    direction: Direction        # BUY or SELL only (NO_TRADE never reaches here)
    entry_mid: float            # ACTUAL entry fill (reference price + adverse slippage)
    sl_price: float             # absolute stop level
    tp_price: float             # absolute target level
    spread_pct: float           # round-trip spread cost basis
    spread_provenance: SpreadProvenance
    slippage_pct: float = 0.0   # adverse slippage baked into entry; also applied on exit
    opened_at: datetime

    @property
    def risk_per_unit(self) -> float:
        """Price distance to the stop — the '1R' unit."""
        return abs(self.entry_mid - self.sl_price)


def open_virtual_trade(
    direction: Direction,
    entry_ref: float,
    sl_pct: float,
    tp_pct: float,
    *,
    spread_pct: float,
    spread_provenance: SpreadProvenance,
    slippage_pct: float = 0.0,
    opened_at: datetime,
) -> VirtualTrade:
    """`entry_ref` is the reference fill price (online: the observed quote; replay: the next
    bar's open). Adverse slippage is applied to it, and SL/TP are derived from that fill."""
    if direction not in (Direction.BUY, Direction.SELL):
        raise ValueError("a virtual trade requires BUY or SELL, not NO_TRADE")
    if entry_ref <= 0 or sl_pct <= 0 or tp_pct <= 0:
        raise ValueError("entry_ref, sl_pct and tp_pct must be positive")
    s = slippage_pct / 100.0
    if direction == Direction.BUY:
        entry_fill = entry_ref * (1 + s)          # buying slips UP (adverse)
        sl = entry_fill * (1 - sl_pct / 100)
        tp = entry_fill * (1 + tp_pct / 100)
    else:  # SELL
        entry_fill = entry_ref * (1 - s)          # selling slips DOWN (adverse)
        sl = entry_fill * (1 + sl_pct / 100)
        tp = entry_fill * (1 - tp_pct / 100)
    return VirtualTrade(
        direction=direction, entry_mid=round(entry_fill, 4), sl_price=round(sl, 4),
        tp_price=round(tp, 4), spread_pct=spread_pct, spread_provenance=spread_provenance,
        slippage_pct=slippage_pct, opened_at=opened_at,
    )
