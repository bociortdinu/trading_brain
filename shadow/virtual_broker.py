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
    entry_mid: float            # mid price at entry (bars are mid)
    sl_price: float             # absolute stop level
    tp_price: float             # absolute target level
    spread_pct: float           # round-trip spread cost basis
    spread_provenance: SpreadProvenance
    opened_at: datetime

    @property
    def risk_per_unit(self) -> float:
        """Price distance to the stop — the '1R' unit."""
        return abs(self.entry_mid - self.sl_price)


def open_virtual_trade(
    direction: Direction,
    entry_mid: float,
    sl_pct: float,
    tp_pct: float,
    *,
    spread_pct: float,
    spread_provenance: SpreadProvenance,
    opened_at: datetime,
) -> VirtualTrade:
    if direction not in (Direction.BUY, Direction.SELL):
        raise ValueError("a virtual trade requires BUY or SELL, not NO_TRADE")
    if entry_mid <= 0 or sl_pct <= 0 or tp_pct <= 0:
        raise ValueError("entry_mid, sl_pct and tp_pct must be positive")
    if direction == Direction.BUY:
        sl = entry_mid * (1 - sl_pct / 100)
        tp = entry_mid * (1 + tp_pct / 100)
    else:  # SELL
        sl = entry_mid * (1 + sl_pct / 100)
        tp = entry_mid * (1 - tp_pct / 100)
    return VirtualTrade(
        direction=direction, entry_mid=entry_mid, sl_price=round(sl, 4), tp_price=round(tp, 4),
        spread_pct=spread_pct, spread_provenance=spread_provenance, opened_at=opened_at,
    )
